"""Board and milestone state recomputed from Flyte runs: the reconciler, the supervisor's direct writes.

Offline: the board is an `httpx.MockTransport`, the App a small fake that keeps posted comments, and
Flyte is `tests/fake_flyte.py`. No test here may touch `flyte.storage` (`no_storage`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import inspect
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import flyte
import flyte.remote
import flyte.storage
import httpx
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from obstore.exceptions import GenericError

from flyte_agent_factory import (
    _board,
    _dashboard,
    _github,
    _issues,
    _profile,
    _pull_requests,
    _session_config,
    _supervise_support,
    _timeline,
)

ISSUE = "example/scratch#8"
URL = "https://console.example/runs/r1"


@pytest.fixture
def no_storage(monkeypatch):
    """Every `flyte.storage` call fails the test: state lives on GitHub and Flyte only."""

    def refuse(*args, **kwargs):
        raise AssertionError("flyte.storage was called")

    for name in dir(flyte.storage):
        if not name.startswith("_") and callable(getattr(flyte.storage, name)):
            monkeypatch.setattr(flyte.storage, name, refuse)


class Board:
    """A Projects v2 board: its fields, its items' Status and text values, and every request it answered."""

    def __init__(self, items: dict[str, dict[str, Any]] | None = None):
        self.fields = [
            {"id": name, "name": name, "options": [{"id": opt, "name": opt} for opt in options]}
            for name, options in _board.SELECTS.items()
        ] + [{"id": name, "name": name, "dataType": "TEXT"} for name in _board.TEXT_FIELDS]
        # ref -> {"status": str, "texts": {field: text}}
        self.items: dict[str, dict[str, Any]] = {
            ref: {"status": "", "texts": {}, **item} for ref, item in (items or {}).items()
        }
        self.deleted: set[str] = set()
        self.writes: list[tuple[str, str, str]] = []  # (ref, field, value)
        self.created: list[str] = []
        self.added: list[str] = []  # refs added with addProjectV2ItemById
        self.issues: set[str] = set()  # refs that exist on GitHub but may not be on the board
        self.unreadable: set[str] = set()  # repos the PAT cannot read: GitHub answers NOT_FOUND
        self.status = 0  # answer every request with this HTTP status, when set
        self.expiry = ""
        self.issue_state = lambda ref: "OPEN"

    def _ref(self, item_id: str) -> str:
        return next(ref for ref in self.items if f"item-{ref}" == item_id)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.status:
            return httpx.Response(self.status)
        body = json.loads(request.content)
        query, variables = body["query"], body["variables"]
        data: dict[str, Any]
        if "ResolveProject" in query:
            nodes = [f for f in self.fields if f["name"] not in self.deleted]
            data = {
                "user": {
                    "projectV2": {"id": "p", "fields": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}
                }
            }
        elif "BoardItem(" in query:
            ref = next((ref for ref in self.items if f"item-{ref}" == variables["id"]), None)
            item = self.items.get(ref) if ref is not None else None
            node = None
            if item is not None:
                node = {
                    "id": variables["id"],
                    "fieldValueByName": {"name": item["status"]},
                    "content": {"state": self.issue_state(ref)},
                    **{field: {"text": text} for field, text in item["texts"].items()},
                }
            data = {"node": node}
        elif "BoardItems" in query:
            nodes = []
            for ref, item in self.items.items():
                repo, number = ref.split("#")
                node = {
                    "id": f"item-{ref}",
                    "fieldValueByName": {"name": item["status"]} if item["status"] else None,
                    "content": {
                        "id": f"issue-{ref}",
                        "number": int(number),
                        "title": "x",
                        "state": self.issue_state(ref),
                        "repository": {"nameWithOwner": repo},
                        "labels": {"nodes": []},
                    },
                }
                for field, text in item["texts"].items():
                    node[field] = {"text": text}
                nodes.append(node)
            data = {"node": {"items": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}}
        elif "IssueNode" in query:
            ref = f"{variables['owner']}/{variables['name']}#{variables['number']}"
            if ref.split("#")[0] in self.unreadable:
                error = {"type": "NOT_FOUND", "path": ["repository"], "message": "Could not resolve"}
                return httpx.Response(200, json={"data": {"repository": None}, "errors": [error]})
            known = ref in self.items or ref in self.issues
            data = {"repository": {"issue": {"id": f"issue-{ref}"} if known else None}}
        elif "AddItem" in query:
            ref = variables["input"]["contentId"].removeprefix("issue-")
            if ref not in self.items:  # GitHub returns the existing item when there is one
                self.items[ref] = {"status": "", "texts": {}}
                self.added.append(ref)
            data = {"addProjectV2ItemById": {"item": {"id": f"item-{ref}"}}}
        elif "SetField" in query:
            value = variables["input"]
            ref, field = self._ref(value["itemId"]), value["fieldId"]
            text = value["value"].get("text", value["value"].get("singleSelectOptionId"))
            self.writes.append((ref, field, text))
            if field == "Status":
                self.items[ref]["status"] = text
            else:
                self.items[ref]["texts"][field] = text
            data = {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": value["itemId"]}}}
        elif "CreateField" in query:
            name = variables["input"]["name"]
            self.created.append(name)
            self.deleted.discard(name)
            data = {
                "createProjectV2Field": {"projectV2Field": next(f for f in self.fields if f["name"] == name)}
            }
        else:
            pytest.fail(f"unexpected board request: {query}")
        headers = {"github-authentication-token-expiration": self.expiry} if self.expiry else {}
        return httpx.Response(200, json={"data": data}, headers=headers)

    def install(self, monkeypatch):
        real = _board.BoardClient
        monkeypatch.setattr(
            _board, "BoardClient", lambda token: real(token, transport=httpx.MockTransport(self.handle))
        )
        return self


class App:
    """The GitHub App's comment calls: what is on each issue, and a status to fail every call with."""

    def __init__(self):
        self.issues: dict[str, list[dict[str, Any]]] = {}
        self.fail = 0
        self.hold: threading.Event | None = None  # while set up and unreleased, every call blocks
        self.lock = threading.Lock()

    def _check(self):
        if self.hold:
            self.hold.wait(60)
        if self.fail:
            raise _github.GithubError(f"github: HTTP {self.fail}", self.fail)

    def comments(self, repo, number):
        self._check()
        return list(self.issues.get(f"{repo}#{number}", []))

    def issue_comments_since(self, repo, number, since=""):
        return self.comments(repo, number)

    def comment(self, repo, number, body):
        self._check()
        with self.lock:
            self.issues.setdefault(f"{repo}#{number}", []).append({"body": body})

    def repo_issues(self, repo, *, max_pages=None):
        self._check()
        return []

    def main_commits(self, repo):
        self._check()
        return []

    def repo_pulls(self, repo):
        self._check()
        return []

    def branch_heads(self, repo):
        self._check()
        return []

    def issue(self, repo, number):
        self._check()
        return {"state": "open"}

    def posted(self, issue=ISSUE) -> list[str]:
        return [c["body"] for c in self.issues.get(issue, [])]

    def keys(self, run, issue=ISSUE) -> list[str]:
        """The milestone keys posted for `run`, in order, read back from their markers."""
        known = ("started", "review-1", "review-2", "review-3", "pr", "ended", "closed-40")
        markers = {_issues.marker(issue, run, key): key for key in known}
        return [key for body in self.posted(issue) for marker, key in markers.items() if marker in body]


def github(app: object) -> _github.GithubApp:
    """A fake App where the code under test wants the real `GithubApp`: it implements the calls made here."""
    return cast(_github.GithubApp, app)


def board_profile(*, app=False):
    from tests.conftest import TEST_PROFILE

    text = TEST_PROFILE.read_text() + (
        '\n[board]\nowner = "example"\nowner_type = "user"\nnumber = 1\npat_secret = "github_board_pat"\n'
    )
    profile = _profile.parse(text)
    if app:
        profile = dataclasses.replace(
            profile, github=_profile.Github(app_id="1", private_key_secret="github_key_x")
        )
    return profile


def snapshot(**parts):
    """A report snapshot with only the parts the reconciler reads."""
    return {
        "schema": _dashboard.SCHEMA,
        "phase": {"kind": parts.pop("phase", "building")},
        "review": {"verdicts": parts.pop("verdicts", [])},
        "deliverables": {"branch": parts.pop("branch", ""), "pr": parts.pop("pr", ""), **parts},
        "cost": {},
    }


# --- desired(): the board values a run implies ----------------------------------------------- #


@pytest.mark.parametrize(
    "live, data, expected",
    [
        (True, None, {"Status": "In flight", "Flyte": URL}),
        (
            True,
            snapshot(branch="factory/widget"),
            {"Status": "In flight", "Flyte": URL, "Branch": "factory/widget", "Engine": "claude"},
        ),
        (True, snapshot(pr="https://github.com/x/y/pull/3"), {"Status": "In review"}),
        (True, snapshot(verdicts=[{"round": 1, "verdict": "PASS"}]), {"Status": "Done"}),
        (False, snapshot(phase="released"), {"Status": "Done"}),
        (
            True,
            snapshot(pr="https://github.com/x/y/pull/3", verdicts=[{"round": 1, "verdict": "PASS"}]),
            {"Status": "In review"},
        ),
        (False, snapshot(pr="https://github.com/x/y/pull/3", phase="released"), {"Status": "In review"}),
        (
            False,
            snapshot(verdicts=[{"round": 1, "verdict": "FAIL"}, {"round": 2, "verdict": "PASS"}]),
            {"Status": "Done"},
        ),
        (False, snapshot(verdicts=[{"round": 1, "verdict": "FAIL"}]), {}),
        (False, snapshot(), {}),
        (False, None, {}),
    ],
)
def test_desired_board_values_from_a_run(live, data, expected):
    got = _board.desired(live, URL, data)
    assert {key: got.get(key) for key in expected} == expected
    if not expected:
        assert got == {}


def test_desired_engine_is_every_engine_the_run_used():
    data = snapshot()
    data["cost"] = {"by_engine": {"codex": 1.0, "claude": 2.0, "unknown": 0.0}}
    assert _board.desired(True, URL, data)["Engine"] == "claude+codex"


# --- the reconciler -------------------------------------------------------------------------- #


def runs(table):
    """`latest(ref)` for the reconciler: a fixed (live, url, snapshot) per issue, else None."""
    return lambda ref: table.get(ref)


def test_reconciler_writes_only_differences_and_leaves_items_without_a_run_alone(monkeypatch, no_storage):
    board = Board(
        {
            "example/scratch#8": {"status": "Ready"},
            "example/scratch#9": {"status": "Backlog"},
            "example/scratch#10": {"status": "Ready"},  # its run ended outside the window: latest is None
            "example/scratch#11": {
                "status": "In flight",
                "texts": {"Flyte": URL, "Branch": "factory/b", "Engine": "claude"},
            },
            "example/scratch#12": {"status": "Done", "texts": {"Flyte": URL, "Engine": "claude"}},
        }
    ).install(monkeypatch)
    latest = runs(
        {
            "example/scratch#8": (True, URL, snapshot(branch="factory/a")),
            "example/scratch#11": (True, URL, snapshot(branch="factory/b")),
            "example/scratch#12": (True, URL, snapshot(phase="released")),
        }
    )
    looked_up = []

    def tracked(ref):
        looked_up.append(ref)
        return latest(ref)

    _board.Reconciler(board_profile(), "token", tracked).tick()
    assert sorted(board.writes) == [
        ("example/scratch#8", "Branch", "factory/a"),
        ("example/scratch#8", "Engine", "claude"),
        ("example/scratch#8", "Flyte", URL),
        ("example/scratch#8", "Status", "In flight"),
    ]
    assert board.items["example/scratch#9"]["status"] == "Backlog"
    assert board.items["example/scratch#10"]["status"] == "Ready"
    assert "example/scratch#12" in looked_up
    board.writes.clear()
    _board.Reconciler(board_profile(), "token", tracked).tick()
    assert board.writes == []


def test_two_reconcilers_in_turn_write_each_value_once(monkeypatch, no_storage):
    board = Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    table = {ISSUE: (True, URL, snapshot())}
    first, second = (_board.Reconciler(board_profile(), "token", runs(table)) for _ in range(2))
    first.tick()
    second.tick()
    table[ISSUE] = (True, URL, snapshot(pr="https://github.com/x/y/pull/3"))
    second.tick()
    first.tick()
    statuses = [value for _, field, value in board.writes if field == "Status"]
    assert statuses == ["In flight", "In review"]
    assert len([w for w in board.writes if w[1] == "Flyte"]) == 1


def test_reconciler_bootstraps_a_deleted_field_then_writes_it(monkeypatch, no_storage):
    rows = []
    board = Board({ISSUE: {"status": "In flight"}}).install(monkeypatch)
    board.deleted = {"Engine"}
    reconciler = _board.Reconciler(
        board_profile(), "token", runs({ISSUE: (True, URL, snapshot())}), lambda *row: rows.append(row)
    )
    reconciler.tick()
    assert board.created == ["Engine"]
    assert (ISSUE, "Engine", "claude") in board.writes
    assert any("Engine" in text for _, text in rows)
    board.created.clear()
    reconciler.tick()
    assert board.created == []  # a board with every field is never bootstrapped


@pytest.mark.parametrize("status, icon", [(401, "🚨"), (502, None)])
def test_a_failing_board_fails_the_tick(monkeypatch, no_storage, status, icon):
    rows = []
    board = Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    board.status = status
    reconciler = _board.Reconciler(
        board_profile(), "token", runs({ISSUE: (True, URL, None)}), lambda *row: rows.append(row)
    )
    with pytest.raises(_board.BoardError) as raised:
        reconciler.tick()
    assert raised.value.status == status
    assert [row[0] for row in rows if row[0] == "🚨"] == ([icon] if icon else [])


def test_one_failing_item_does_not_stop_the_others_and_still_fails_the_tick(monkeypatch, no_storage):
    board = Board({"example/scratch#7": {"status": "Ready"}, ISSUE: {"status": "Ready"}}).install(monkeypatch)

    def latest(ref):
        if ref == "example/scratch#7":
            raise _board.BoardError("board HTTP 502", 502)
        return (True, URL, None)

    with pytest.raises(_board.BoardError):
        _board.Reconciler(board_profile(), "token", latest).tick()
    assert (ISSUE, "Status", "In flight") in board.writes


def test_reconciler_warns_of_pat_expiry_from_the_response_header(monkeypatch, no_storage):
    rows = []
    board = Board({}).install(monkeypatch)
    board.expiry = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S UTC")
    _board.Reconciler(board_profile(), "token", runs({}), lambda *row: rows.append(row)).tick()
    assert [icon for icon, text in rows if "expires" in text] == ["🚨"]


# --- the supervisor writes directly, retrying in memory -------------------------------------- #


def test_supervisor_move_writes_directly(monkeypatch, no_storage):
    board = Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    worker = _board.supervisor(board_profile(), "token", ISSUE, lambda *row: None)
    assert worker is not None
    worker.move("In flight", Flyte=URL, Branch="factory/x", Engine="claude")
    assert board.writes[-1] == (ISSUE, "Status", "In flight")
    assert {field for _, field, _ in board.writes} == {"Status", *_board.TEXT_FIELDS}
    assert not worker.pending


def test_a_launch_for_an_issue_not_on_the_board_adds_it_then_moves_it_to_in_flight(monkeypatch, no_storage):
    board = Board().install(monkeypatch)
    board.issues.add(ISSUE)
    worker = _board.supervisor(board_profile(), "token", ISSUE, lambda *row: None)
    assert worker is not None
    worker.move("In flight", Flyte=URL)
    assert board.added == [ISSUE]
    assert board.items[ISSUE]["status"] == "In flight"
    assert board.items[ISSUE]["texts"]["Flyte"] == URL
    assert not worker.pending


def test_an_issue_already_on_the_board_is_not_added_again(monkeypatch, no_storage):
    board = Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    worker = _board.supervisor(board_profile(), "token", ISSUE, lambda *row: None)
    assert worker is not None
    worker.move("In flight", Flyte=URL)
    worker.move("In review")
    assert board.added == []
    assert board.items[ISSUE]["status"] == "In review"


def test_the_reconciler_adds_a_missing_item_for_a_live_run(monkeypatch, no_storage):
    board = Board({"example/scratch#9": {"status": "Ready"}}).install(monkeypatch)
    board.issues.add(ISSUE)
    latest = runs({ISSUE: (True, URL, snapshot(branch="factory/a"))})
    _board.Reconciler(
        board_profile(), "token", latest, live_issues=lambda: [ISSUE, "example/scratch#9"]
    ).tick()
    assert board.added == [ISSUE]
    assert board.items[ISSUE]["status"] == "In flight"
    assert board.items[ISSUE]["texts"]["Flyte"] == URL
    board.writes.clear()
    _board.Reconciler(board_profile(), "token", latest, live_issues=lambda: [ISSUE]).tick()
    assert board.added == [ISSUE] and board.writes == []


def test_an_unreadable_repo_is_reported_as_unaddable_naming_the_issue(monkeypatch, no_storage):
    rows = []
    board = Board().install(monkeypatch)
    board.unreadable.add("example/private")
    worker = _board.supervisor(board_profile(), "token", "example/private#4", lambda *row: rows.append(row))
    assert worker is not None
    worker.move("In flight", Flyte=URL)
    assert worker.pending
    (row,) = rows
    assert "example/private#4 could not be added" in row[1] and "read its repository" in row[1]


def test_an_unreadable_repo_does_not_fail_the_tick_and_is_alerted_once(monkeypatch, no_storage):
    rows = []
    private = "example/private#4"
    board = Board({"example/scratch#9": {"status": "Ready"}}).install(monkeypatch)
    board.issues.add(ISSUE)
    board.unreadable.add("example/private")
    latest = runs({ISSUE: (True, URL, snapshot(branch="factory/a")), "example/scratch#9": (True, URL, None)})
    reconciler = _board.Reconciler(
        board_profile(), "token", latest, lambda *row: rows.append(row), live_issues=lambda: [private, ISSUE]
    )
    reconciler.tick()
    reconciler.tick()  # must not raise
    assert board.items[ISSUE]["status"] == "In flight"
    assert board.items["example/scratch#9"]["status"] == "In flight"
    assert [text for _, text in rows if private in text and "could not be added" in text] != []
    assert len([1 for _, text in rows if private in text]) == 1
    assert not [1 for _, text in rows if "Board sync failed" in text]


def test_other_add_failures_still_fail_the_tick(monkeypatch, no_storage):
    real = _board.BoardClient  # before install replaces it
    board = Board().install(monkeypatch)
    board.issues.add(ISSUE)
    original = board.handle

    def flaky(request):
        if b"AddItem" in request.content:
            return httpx.Response(502)
        return original(request)

    monkeypatch.setattr(
        _board, "BoardClient", lambda token: real(token, transport=httpx.MockTransport(flaky))
    )
    with pytest.raises(_board.BoardError):
        _board.Reconciler(board_profile(), "token", runs({}), live_issues=lambda: [ISSUE]).tick()


def test_supervisor_failed_move_is_retried_in_memory(monkeypatch, no_storage):
    rows = []
    board = Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    board.status = 502
    worker = _board.supervisor(board_profile(), "token", ISSUE, lambda *row: rows.append(row))
    assert worker is not None
    worker.move("In flight", Flyte=URL)
    worker.move("In review")
    worker.tick()
    assert worker.pending and board.writes == []
    assert [row[0] for row in rows] == ["⚠️"]  # one row per failing streak
    board.status = 0
    worker.tick()
    # The latest move, with the texts of the earlier one.
    assert board.items[ISSUE] == {"status": "In review", "texts": {"Flyte": URL}}
    assert not worker.pending
    board.writes.clear()  # type: ignore[unreachable]  # mypy keeps the earlier `pending` narrowing across tick()
    worker.tick()
    assert board.writes == []


async def test_board_loop_retries_a_failed_move(monkeypatch, no_storage):

    board = Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    board.status = 502
    worker = _board.supervisor(board_profile(), "token", ISSUE, lambda *row: None)
    assert worker is not None
    monkeypatch.setattr(_supervise_support, "_board_worker", worker)
    monkeypatch.setattr(_supervise_support, "_BOARD_EVERY_S", 0.01)
    timeline = _timeline._Timeline()
    await _supervise_support._move_board("In flight", timeline, Flyte=URL)
    assert worker.pending
    board.status = 0
    stop = asyncio.Event()
    loop = asyncio.create_task(_supervise_support._board_loop(timeline, stop))
    for _ in range(1000):
        if not worker.pending:
            break  # type: ignore[unreachable]  # mypy keeps the earlier `pending` narrowing across tick()
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(loop, timeout=30)
    assert board.items[ISSUE]["status"] == "In flight"


# --- milestones: check, then post -------------------------------------------------------------- #


def test_a_present_marker_is_not_posted_again(no_storage):
    app = App()
    assert _issues.post_milestone(github(app), ISSUE, "r1", "started", "Started: run") is True
    assert _issues.post_milestone(github(app), ISSUE, "r1", "started", "Started: run") is False
    assert app.keys("r1") == ["started"]
    # A marker the old outbox drainer posted is the same marker.
    digest = hashlib.sha256(f"{ISSUE}:r1:started".encode()).hexdigest()
    assert _issues.marker(ISSUE, "r1", "started") == f"<!-- factory-issue:{digest} -->"


def test_a_5xx_keeps_the_milestone_in_memory_and_the_next_flush_posts_it(no_storage):
    app = App()
    app.fail = 503
    milestones = _issues.Milestones(github(app), ISSUE, "r1")
    milestones.milestone("started", "Started: run")
    assert milestones.flush() is False
    assert "started" in milestones.pending
    app.fail = 0
    assert milestones.flush() is True
    assert milestones.flush() is True
    assert app.keys("r1") == ["started"]


def test_milestones_post_eagerly_without_waiting_for_a_flush(no_storage):
    app = App()
    milestones = _issues.Milestones(github(app), ISSUE, "r1")
    milestones.milestone("started", "Started: run")
    for _ in range(200):
        if app.posted():
            break
        time.sleep(0.01)
    assert app.keys("r1") == ["started"]


async def test_the_final_flush_is_bounded(monkeypatch, no_storage):

    app = App()
    app.hold = threading.Event()
    monkeypatch.setattr(_supervise_support, "_MILESTONE_FLUSH_S", 0.2)
    timeline = _timeline._Timeline()
    timeline.issues = _issues.Milestones(github(app), ISSUE, "r1")
    timeline.issues.pending["started"] = "Started: run"  # queued, never kicked: only the flush posts it
    # The call stays blocked (up to 60 s), so returning at all is the bound: 20 s is a hang, not the 0.2 s.
    try:
        await asyncio.wait_for(_supervise_support._flush_milestones(timeline), 30)
    finally:
        app.hold.set()


# --- the reconciler's milestone heal --------------------------------------------------------- #


@pytest.fixture
def world(flyte_world, monkeypatch):
    from flyte_agent_factory import _factory, _factory_base

    profile = dataclasses.replace(
        _factory_base.PROFILE,
        repo="git@github.com:you/repo.git",
        repos={"project": "git@github.com:you/repo.git"},
        github=_profile.Github(app_id="1", private_key_secret="github_key_x"),
    )
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setattr(_factory, "REPO", profile.repo)
    flyte_world.profile = profile
    return flyte_world


def ended(run, hours_ago):
    from tests.fake_flyte import ended_at

    ended_at(run, datetime.now(timezone.utc) - timedelta(hours=hours_ago))


def test_reconciler_posts_derived_milestones_once_and_skips_fresh_keys_of_live_runs(world):
    from flyte_agent_factory import _factory, _reconcile

    now = time.time()
    # Two slots: each ended run frees one for the next launch.
    old = _factory.launch_project("brief", "old-widget", issue="you/repo#14")["run"]
    world.runs[old].phase = "SUCCEEDED"
    done = _factory.launch_project("brief", "done-widget", issue="you/repo#12")["run"]
    world.runs[done].phase = "SUCCEEDED"
    live = _factory.launch_project("brief", "live-widget", issue="you/repo#13")["run"]
    pr = "https://github.com/you/repo/pull/3"
    ended(world.runs[done], 1)
    world.runs[done].report = _dashboard.status_script(
        snapshot(
            verdicts=[
                {"round": 1, "verdict": "FAIL", "blocking": 2, "at": now - 7200},
                {"round": 2, "verdict": "PASS", "blocking": 0, "at": now - 3700},
            ],
            pr=pr,
            pr_at=now - 7300,
            phase="released",
        )
    )
    world.runs[live].report = _dashboard.status_script(
        snapshot(
            verdicts=[{"round": 1, "verdict": "FAIL", "blocking": 1, "at": now - 30}], pr=pr, pr_at=now - 60
        )
    )
    ended(world.runs[old], 30)
    app = App()
    for _ in range(2):
        _reconcile.heal_milestones(world.profile, github(app), now=now)
    assert app.keys(done, "you/repo#12") == ["started", "review-1", "review-2", "pr", "ended"]
    # Live: started is old enough to heal; the review and PR are the supervisor's for 10 minutes.
    assert app.keys(live, "you/repo#13") == ["started"]
    assert app.posted("you/repo#14") == []
    bodies = app.posted("you/repo#12")
    assert any(body.startswith("QA round 2: PASS; 0 blocking finding(s).") for body in bodies)
    assert any(body.startswith(f"PR opened: {pr}") for body in bodies)


def test_a_failing_app_fails_the_milestone_heal(world):
    from flyte_agent_factory import _factory, _reconcile

    _factory.launch_project("brief", "widget", issue="you/repo#12")
    app = App()
    app.fail = 502
    with pytest.raises(_github.GithubError):
        _reconcile.heal_milestones(world.profile, github(app))


def test_latest_run_is_live_or_ended_inside_the_window(world):
    from flyte_agent_factory import _factory, _reconcile

    run = world.runs[_factory.launch_project("brief", "widget", issue="you/repo#12")["run"]]
    run.report = _dashboard.status_script(snapshot(branch="factory/widget"))
    latest = _reconcile.latest_run("you/repo#12")
    assert latest is not None
    live, url, data = latest
    assert (
        live
        and url == _board.run_url(_reconcile.PROFILE, run.name)
        # `branch` is in the test's snapshot but not in the typed `_Deliverables` shape.
        and cast(Any, data)["deliverables"]["branch"] == "factory/widget"
    )
    run.phase = "SUCCEEDED"
    ended(run, 1)
    latest = _reconcile.latest_run("you/repo#12")
    assert latest is not None and latest[0] is False
    ended(run, 3)
    assert _reconcile.latest_run("you/repo#12") is None
    assert _reconcile.latest_run("you/repo#99") is None


def test_ended_at_reads_a_real_run_proto():
    from flyte.remote import Run
    from flyteidl2.workflow import run_definition_pb2  # type: ignore[import-untyped]  # ships no stubs

    from flyte_agent_factory import _reconcile

    pb2 = run_definition_pb2.Run()
    pb2.action.id.name = "a0"
    run = Run(pb2=pb2)
    assert _reconcile._ended_at(run) is None
    pb2.action.status.end_time.FromDatetime(datetime(2026, 10, 1, 12, 0))
    assert _reconcile._ended_at(Run(pb2=pb2)) == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


# --- step 1 acceptance: a project's board moves and milestones without object storage -------- #


async def _project_moves(agent, timeline, worker_issue, pr):
    """What `project` does to the board and the issue, in order, from start to release."""
    timeline.issues.milestone("started", f"Started: {timeline.run_url}")
    await _supervise_support._start_board(worker_issue, "widget", "token", timeline)
    timeline.issues.milestone("review-1", _issues.review_body(1, "FAIL", 2))
    timeline.row("🔀", f"pull request {pr}")
    timeline.pr_url = pr
    await _supervise_support._move_board("In review", timeline, Engine="claude")
    timeline.issues.milestone("review-2", _issues.review_body(2, "PASS", 0))
    await _supervise_support._move_board("Done", timeline, Engine="claude")
    timeline.row("🏁", "released after 30m idle having passed review in round 2")


@pytest.mark.parametrize("outage", [False, True])
async def test_a_project_moves_its_item_and_posts_each_milestone_once_without_storage(
    monkeypatch, no_storage, outage
):
    from flyte_agent_factory import agent

    board = Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    app = App()
    profile = board_profile(app=True)
    # `_start_board` sets the module global; restored after the test, so no later test writes to a
    # board through it.
    monkeypatch.setattr(_supervise_support, "_board_worker", None)
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    monkeypatch.setattr(_supervise_support, "_BOARD_EVERY_S", 0.01)
    timeline = _timeline._Timeline()
    timeline.run_url = URL
    timeline.issues = _issues.for_project(profile, "widget", "r1", github(app), issue=ISSUE)
    assert timeline.issues is not None
    if outage:
        board.status = app.fail = 502
    await _project_moves(agent, timeline, ISSUE, "https://github.com/example/scratch/pull/3")
    if outage:
        assert board.writes == [] and timeline.issues.pending
        board.status = app.fail = 0
        stop = asyncio.Event()
        loop = asyncio.create_task(_supervise_support._board_loop(timeline, stop))
        assert _supervise_support._board_worker is not None
        for _ in range(1000):
            if not (_supervise_support._board_worker.pending or timeline.issues.pending):
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(loop, timeout=30)
    await _supervise_support._flush_milestones(timeline)
    assert board.items[ISSUE]["status"] == "Done"
    statuses = [value for _, field, value in board.writes if field == "Status"]
    assert statuses == (["Done"] if outage else ["In flight", "In review", "Done"])
    assert sorted(app.keys("r1")) == sorted(["started", "review-1", "pr", "review-2", "ended"])


# --- deploy: the reconciler replaces two triggers --------------------------------------------- #


def test_reconcile_task_is_named_as_deployed_and_mounts_the_app_key():
    import os
    import subprocess
    import sys

    from flyte_agent_factory import _deploy, _pull_requests

    profile = board_profile(app=True)
    env = {key: value for key, value in os.environ.items() if key != _profile.PROFILE_ENV}
    env[_profile.TOML_ENV] = profile.text.replace(
        "[board]", '[github]\napp_id = "1"\nprivate_key_secret = "github_key_x"\n\n[board]'
    )
    code = f"""
from flyte_agent_factory import _reconcile
assert _reconcile.reconcile.name == {_deploy.RECONCILE_TASK!r}, _reconcile.reconcile.name
assert [(s.key, s.as_env_var) for s in _reconcile.env.secrets] == [
    ("github_board_pat", "GITHUB_BOARD_PAT"), ("github_key_x", {_pull_requests.KEY_ENV_VAR!r})
], _reconcile.env.secrets
"""
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]


def test_reconcile_runs_are_not_projects(flyte_world):
    from flyte_agent_factory import _deploy, _factory, _factory_base

    flyte_world.add("tick", _deploy.RECONCILE_TASK)
    project = flyte_world.add("work", _factory_base.SLOTS[0])
    assert [r.name for r in _factory.live_runs()] == [project.name]


@pytest.mark.parametrize(
    "webhook, board, deleted", [(True, True, 2), (True, False, 2), (False, True, 2), (False, False, 2)]
)
def test_retired_triggers_are_deleted_where_the_profile_deployed_them(monkeypatch, webhook, board, deleted):
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError
    from flyte import remote

    from flyte_agent_factory import _deploy

    calls = []

    def delete(name, task_name):
        calls.append((name, task_name))
        # Already gone (an earlier deploy deleted it): fine.
        raise ConnectError(Code.NOT_FOUND, "trigger not found")

    monkeypatch.setattr(remote.Trigger, "delete", delete)
    profile = dataclasses.replace(
        board_profile(),
        board=board_profile().board if board else None,
        github=_profile.Github(webhook_secret="hook" if webhook else ""),
    )
    _deploy.delete_retired_triggers(profile)
    assert len(calls) == deleted
    assert set(calls) == set(_deploy.RETIRED_TRIGGERS)
    if deleted:

        def unavailable(name, task_name):
            raise ConnectError(Code.UNAVAILABLE, "down")

        monkeypatch.setattr(remote.Trigger, "delete", unavailable)
        _deploy.delete_retired_triggers(profile)
        assert len(calls) == 2


def test_retired_triggers_name_the_tasks_that_carried_them():
    from flyte_agent_factory import _deploy

    assert dict(_deploy.RETIRED_TRIGGERS) == {
        "issue_outbox": "factory-issues.drain_outbox",
        "factory-board-sync": "factory-board-sync.sync_board",
    }


def test_state_report_counts_the_old_outboxes_and_lists_what_will_not_heal(tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "state_report", Path(__file__).resolve().parents[1] / "ops/state_report.py"
    )
    assert spec is not None and spec.loader is not None
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    board = tmp_path / "board/outbox"
    board.mkdir(parents=True)
    bootstrap = {"op": "bootstrap", "target": "project", "value": {}, "key": "b" * 64}
    for n in range(300):
        (board / f"{n}.json").write_text(json.dumps(bootstrap))
    (board / "x.json").write_text("{trunc")
    github = tmp_path / "github/outbox"
    github.mkdir(parents=True)
    now = 100_000.0
    (github / "a.json").write_text(
        json.dumps({"issue": "o/r#1", "body": "Started: run", "created": now - 60})
    )
    (github / "b.json").write_text(
        json.dumps({"issue": "o/r#1", "body": "Blocked/escalated: checks\nfailing", "created": now - 60})
    )
    lines = report.report(tmp_path.as_uri(), now)
    assert "board/outbox: 301 pending record(s), 2 distinct" in lines
    assert f"  300 x bootstrap {'b' * 12}" in lines
    assert any("re-derived: Started: run" in line and "NOT" not in line for line in lines)
    assert any("NOT re-derived: Blocked/escalated: checks" in line for line in lines)
    assert "    Blocked/escalated: checks\n    failing" in lines
    # Read-only: nothing was moved, written or removed.
    assert len(list(board.iterdir())) == 301 and len(list(github.iterdir())) == 2


def test_missing_report_is_an_empty_snapshot_on_the_board(world, monkeypatch):
    from flyte_agent_factory import _factory, _reconcile

    first = world.runs[_factory.launch_project("brief", "first", issue="you/repo#12")["run"]]
    _factory.launch_project("brief", "second", issue="you/repo#13")
    first.report = None  # the real get_report raises RuntimeError("No report is available")
    board = Board({"you/repo#12": {"status": "Ready"}, "you/repo#13": {"status": "Ready"}}).install(
        monkeypatch
    )
    rows = []
    reconciler = _board.Reconciler(board_profile(), "token", _reconcile.latest_run, lambda *r: rows.append(r))
    for _ in range(2):
        reconciler.tick()
    assert board.items["you/repo#12"]["status"] == "In flight"
    assert board.items["you/repo#13"]["status"] == "In flight"
    assert rows == []


def test_a_run_that_never_started_is_not_the_boards_latest_run(world, monkeypatch):
    from flyte_agent_factory import _factory, _reconcile

    run = world.runs[_factory.launch_project("brief", "queued", issue="you/repo#12")["run"]]
    run.phase = "ABORTED"
    run.report = None
    ended(run, 1)
    monkeypatch.setattr(_reconcile, "_started_at", lambda run: None)
    assert _reconcile.latest_run("you/repo#12") is None
    board = Board({"you/repo#12": {"status": "Ready"}}).install(monkeypatch)
    _board.Reconciler(board_profile(), "token", _reconcile.latest_run).tick()
    assert board.writes == []


@pytest.mark.parametrize("error", [RuntimeError("No report is available"), OSError("unavailable")])
def test_missing_report_does_not_stop_later_milestone_runs(world, monkeypatch, caplog, error):
    from flyte_agent_factory import _factory, _reconcile

    first = world.runs[_factory.launch_project("brief", "first", issue="you/repo#12")["run"]]
    second = _factory.launch_project("brief", "second", issue="you/repo#13")["run"]

    def unavailable():
        raise error

    monkeypatch.setattr(first, "get_report", unavailable)
    app = App()
    monkeypatch.setattr(_reconcile, "_missing_reports", set(), raising=False)
    started = _reconcile._started_at(first)
    assert started is not None
    monkeypatch.setattr(time, "time", lambda: started + 3630)
    for _ in range(2):
        _reconcile.heal_milestones(world.profile, github(app))
    assert app.keys(first.name, "you/repo#12") == []
    assert app.keys(second, "you/repo#13") == ["started"]
    assert sum("report" in record.message for record in caplog.records) == 1
    monkeypatch.setattr(first, "get_report", lambda: "")
    _reconcile.heal_milestones(world.profile, github(app))
    monkeypatch.setattr(first, "get_report", unavailable)
    _reconcile.heal_milestones(world.profile, github(app))
    assert sum("report" in record.message for record in caplog.records) == 2


@pytest.mark.parametrize(
    "error",
    [
        ConnectError(Code.UNAVAILABLE, "offline"),
        RuntimeError("report fetch failed"),
        OSError("offline"),
        GenericError("offline"),
        httpx.ReadError("offline"),
        ValueError("bad report"),
        KeyError("report"),
    ],
)
async def test_a_report_error_does_not_fail_a_tick_with_a_board(world, monkeypatch, error):
    from flyte_agent_factory import _factory, _reconcile

    first = world.runs[_factory.launch_project("brief", "first", issue="you/repo#12")["run"]]
    second = _factory.launch_project("brief", "second", issue="you/repo#13")["run"]

    def unavailable():
        raise error

    monkeypatch.setattr(first, "get_report", unavailable)
    board = Board({"you/repo#12": {"status": "Ready"}, "you/repo#13": {"status": "Ready"}}).install(
        monkeypatch
    )
    app = App()
    monkeypatch.setattr(
        _reconcile, "PROFILE", dataclasses.replace(world.profile, board=board_profile().board)
    )
    monkeypatch.setattr(_board, "take_token", lambda *a: "token")
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda *a: "key")
    monkeypatch.setattr(_github, "GithubApp", lambda *a: app)
    started = _reconcile._started_at(first)
    assert started is not None
    monkeypatch.setattr(time, "time", lambda: started + 3630)
    await _reconcile.reconcile()
    assert board.items["you/repo#12"]["status"] == "In flight"
    assert board.items["you/repo#13"]["status"] == "In flight"
    assert app.keys(first.name, "you/repo#12") == []
    assert app.keys(second, "you/repo#13") == ["started"]


def test_missing_report_warnings_are_hourly_across_module_reloads(world, caplog):
    from flyte_agent_factory import _factory, _reconcile

    run = world.runs[_factory.launch_project("brief", "first", issue="you/repo#12")["run"]]
    run.report = None
    started = _reconcile._started_at(run)
    assert started is not None
    app = App()
    for age, warnings in [(30, 1), (330, 1), (3630, 2), (3930, 2)]:
        importlib.reload(_reconcile)
        _reconcile.heal_milestones(world.profile, github(app), now=started + age, runs=[run])
        assert sum("report" in record.message for record in caplog.records) == warnings


@pytest.mark.parametrize("kind", ["review", "building", "waiting"])
def test_a_new_review_supersedes_the_previous_pass(kind):
    data = snapshot(pr="https://github.com/x/y/pull/3", verdicts=[{"round": 1, "verdict": "PASS", "at": 10}])
    data["phase"] = {"kind": kind, "round": 2, "since": 20}
    data["review"].update(round=2, started_at=20)
    assert _board.desired(True, URL, data)["Status"] == "In review"


def test_a_done_item_moves_back_for_a_later_review(monkeypatch):
    board = Board({ISSUE: {"status": "Done"}}).install(monkeypatch)
    data = snapshot(pr="https://github.com/x/y/pull/3", verdicts=[{"round": 1, "verdict": "PASS"}])
    data["review"].update(round=2, started_at=20)
    _board.Reconciler(board_profile(), "token", runs({ISSUE: (True, URL, data)})).tick()
    assert board.items[ISSUE]["status"] == "In review"
    data["review"]["verdicts"].append({"round": 2, "verdict": "FAIL"})
    _board.Reconciler(board_profile(), "token", runs({ISSUE: (True, URL, data)})).tick()
    assert board.items[ISSUE]["status"] == "In review"


def test_milestone_reads_are_bounded_by_the_supervisor_start(monkeypatch):
    app = App()
    seen: list[str] = []

    def issue_comments_since(repo, number, since=""):
        seen.append(since)
        return []

    monkeypatch.setattr(app, "issue_comments_since", issue_comments_since)
    worker = _issues.Milestones(github(app), ISSUE, "r1", started_at=1000)
    worker.pending["started"] = "Started"
    worker.flush()
    assert seen == [datetime.fromtimestamp(1000, timezone.utc).isoformat()]


def test_unstarted_runs_have_no_milestones(world, monkeypatch):
    from flyte_agent_factory import _factory, _reconcile

    run = world.runs[_factory.launch_project("brief", "queued", issue="you/repo#12")["run"]]
    run.phase = "ABORTED"
    ended(run, 1)
    monkeypatch.setattr(_reconcile, "_started_at", lambda run: None)
    monkeypatch.setattr(_reconcile, "_snapshot", lambda run: pytest.fail("read an unstarted run's report"))
    app = App()
    assert _reconcile.heal_milestones(world.profile, github(app)) == 0
    assert _issues.derive(None, url=URL, phase="ABORTED", live=False, started_at=None) == []


async def test_supervisor_then_reconcile_makes_no_extra_writes(monkeypatch):

    board = Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    profile = board_profile()
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    monkeypatch.setattr(_supervise_support, "_board_worker", None)
    monkeypatch.setattr(_session_config, "_load_usage", lambda: None)
    monkeypatch.setattr(_session_config, "_usage", {"qa": {"engines": ["codex", "unknown"]}})
    timeline = _timeline._Timeline()
    timeline.run_url = URL
    timeline.branch = "factory/widget"
    await _supervise_support._start_board(ISSUE, "widget", "token", timeline)
    data = snapshot(branch=timeline.branch)
    data["cost"] = {"by_engine": {"codex+unknown": 1}}
    board.writes.clear()
    _board.Reconciler(profile, "token", runs({ISSUE: (True, URL, data)})).tick()
    assert board.writes == []


@pytest.mark.parametrize("code", ["UNAVAILABLE", "UNAUTHENTICATED", "PERMISSION_DENIED"])
def test_retired_trigger_cleanup_warns_except_for_auth(monkeypatch, caplog, code):
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError
    from flyte import remote

    from flyte_agent_factory import _deploy

    calls = []

    def delete(**kwargs):
        calls.append(kwargs)
        raise ConnectError(getattr(Code, code), "cleanup failed")

    monkeypatch.setattr(remote.Trigger, "delete", delete)
    if code == "UNAVAILABLE":
        _deploy.delete_retired_triggers(board_profile())
        assert len(calls) == 2
        assert "retired trigger" in caplog.text
    else:
        with pytest.raises(ConnectError):
            _deploy.delete_retired_triggers(board_profile())


async def test_board_remote_failure_does_not_skip_milestone_healing(world, monkeypatch):
    from flyte_agent_factory import _reconcile

    healed: list[bool] = []

    def heal(*args, **kwargs):
        healed.append(True)
        return 0

    monkeypatch.setattr(_reconcile, "PROFILE", board_profile(app=True))
    monkeypatch.setattr(_board, "take_token", lambda *a: "token")
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda *a: "key")
    monkeypatch.setattr(_github, "GithubApp", lambda *a: App())
    monkeypatch.setattr(_reconcile, "project_runs", list)
    monkeypatch.setattr(_reconcile, "heal_milestones", heal)

    def fail(self):
        raise RuntimeError("No report is available")

    monkeypatch.setattr(_board.Reconciler, "tick", fail)
    with pytest.raises(RuntimeError, match="No report"):
        await _reconcile.reconcile()
    assert healed == [True]


def test_supervisor_and_reconciler_use_the_same_run_url(world):
    from flyte_agent_factory import _factory, _factory_base, _reconcile

    name = _factory.launch_project("brief", "widget", issue="you/repo#12")["run"]
    latest = _reconcile.latest_run("you/repo#12")
    assert latest is not None
    assert latest[1] == _board.run_url(_factory_base.PROFILE, name)


def test_concurrent_milestone_writers_delete_only_their_own_duplicate():
    class RacingApp(App):
        def __init__(self):
            super().__init__()
            self.barrier = threading.Barrier(2)
            self.reads = 0
            self.deleted = []

        def issue_comments_since(self, repo, number, since=""):
            with self.lock:
                self.reads += 1
                first_read = self.reads <= 2
            if first_read:
                self.barrier.wait(timeout=5)
                return []
            return self.comments(repo, number)

        def comment(self, repo, number, body):
            with self.lock:
                comments = self.issues.setdefault(f"{repo}#{number}", [])
                result = {"id": len(comments) + 1, "body": body}
                comments.append(result)
                return result

        def delete_comment(self, repo, comment_id):
            with self.lock:
                self.deleted.append(comment_id)
                self.issues[ISSUE] = [c for c in self.issues[ISSUE] if c["id"] != comment_id]

    from concurrent.futures import ThreadPoolExecutor

    app = RacingApp()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_issues.post_milestone, github(app), ISSUE, "r1", "started", "Started")
            for _ in range(2)
        ]
        for future in futures:
            future.result(timeout=5)
    assert len(app.posted()) == 1
    assert app.deleted == [2]


def test_reconcile_request_budget_with_a_new_app_each_tick(world, monkeypatch):
    from flyte_agent_factory import _factory, _reconcile
    from tests.test_github import PRIVATE_PEM, Clock, FakeGithub

    name = _factory.launch_project("brief", "widget", issue="you/repo#12")["run"]
    run = world.runs[name]
    run.phase = "SUCCEEDED"
    ended(run, 1)
    run.report = _dashboard.status_script(
        snapshot(
            phase="released",
            branch="factory/widget",
            pr="https://github.com/you/repo/pull/3",
            verdicts=[{"round": 1, "verdict": "FAIL"}, {"round": 2, "verdict": "PASS"}],
        )
    )
    board = Board({"you/repo#12": {"status": "Ready"}})
    graphql = []
    original_handle = board.handle

    def board_handle(request):
        graphql.append(json.loads(request.content)["query"])
        return original_handle(request)

    monkeypatch.setattr(board, "handle", board_handle)
    board.install(monkeypatch)
    comments: list[Any] = []
    rest: list[Any] = []
    auth = FakeGithub(Clock())

    def github_handle(request):
        rest.append((request.method, request.url.path))
        if request.url.path.endswith("/comments"):
            if request.method == "GET":
                assert request.url.params["since"]
                assert "If-None-Match" not in request.headers
                return httpx.Response(200, json=list(comments))
            comment = {"id": len(comments) + 1, **json.loads(request.content)}
            comments.append(comment)
            return httpx.Response(201, json=comment)
        # The auth fake's installation endpoint is for a neutral repository slug.
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 77})
        return auth.handler(request)

    counts = []
    for _ in range(2):
        graphql.clear()
        rest.clear()
        _board.Reconciler(board_profile(), "token", _reconcile.latest_run).tick()
        app = _github.GithubApp(
            "1",
            PRIVATE_PEM,
            client=httpx.Client(base_url=_github.API, transport=httpx.MockTransport(github_handle)),
            clock=Clock(),
        )
        _reconcile.heal_milestones(world.profile, github(app))
        counts.append((len(graphql), len(rest), sum(method == "GET" for method, _ in rest)))
    # +1 GraphQL on the first tick (board #55): the one card that needs a write is re-read under
    # WRITE_LOCK first, so a completion written since the listing is not undone. A synced tick
    # (the second) makes no reread.
    assert counts == [(7, 17, 11), (2, 7, 6)]
    assert len(comments) == 5


def test_missing_board_item_recovers_without_restarting_the_supervisor(monkeypatch):
    board = Board({}).install(monkeypatch)
    rows = []
    worker = _board.supervisor(board_profile(), "token", ISSUE, lambda *row: rows.append(row))
    assert worker is not None
    worker.move("In flight", Flyte=URL)
    worker.tick()
    assert worker.pending
    assert len(rows) == 1 and rows[0][0] == "🚨"
    board.items[ISSUE] = {"status": "Ready", "texts": {}}
    worker.tick()
    assert not worker.pending
    assert board.items[ISSUE] == {"status": "In flight", "texts": {"Flyte": URL}}  # type: ignore[unreachable]  # mypy keeps the earlier `pending` narrowing across tick()


def test_invalid_bootstrap_retries_without_writing_items(monkeypatch):
    board = Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    size = next(field for field in board.fields if field["name"] == "Size")
    options = size.pop("options")
    rows = []
    reconciler = _board.Reconciler(
        board_profile(), "token", runs({ISSUE: (True, URL, None)}), lambda *r: rows.append(r)
    )
    for _ in range(3):
        with pytest.raises(_board.BoardError) as exc:
            reconciler.tick()
        assert exc.value.status == 400
        assert board.writes == []
    assert sum(icon == "🚨" for icon, _ in rows) == 1
    size["options"] = options
    reconciler.tick()
    assert board.items[ISSUE]["status"] == "In flight"
    board.status = 401
    with pytest.raises(_board.BoardError):
        reconciler.tick()
    assert sum(icon == "🚨" for icon, _ in rows) == 2


def test_a_review_started_after_pass_is_newer_even_when_its_round_number_repeats():
    data = snapshot(verdicts=[{"round": 1, "verdict": "PASS", "at": 10}])
    data["review"].update(round=1, started_at=20)
    assert _board.desired(True, URL, data)["Status"] == "In review"


# --- one listing per tick, not one per board item ---------------------------------------------- #


async def test_a_tick_lists_runs_once_per_slot_however_many_items_the_board_has(world, monkeypatch):
    from flyte_agent_factory import _factory_base, _reconcile

    slot = _factory_base.SLOTS[0]
    labels = lambda ref: {_reconcile.LABEL_ISSUE: _issues.issue_label(ref)}  # noqa: E731 -- a one-line fake, clearer inline
    world.add("old", slot, phase="SUCCEEDED", labels=labels("you/repo#1"))
    ended(world.runs["old"], 5)
    world.add("live-a", slot, labels=labels("you/repo#2"))
    world.add("live-b", slot, labels=labels("you/repo#3"))
    world.runs["live-a"].report = _dashboard.status_script(snapshot(branch="factory/a"))
    world.runs["live-b"].report = None
    board = Board({f"you/repo#{n}": {"status": "Ready"} for n in range(1, 201)}).install(monkeypatch)
    monkeypatch.setattr(_reconcile, "PROFILE", board_profile())
    monkeypatch.setattr(_board, "take_token", lambda *a: "token")
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda *a: "")
    world.listings = 0
    await _reconcile.reconcile()
    assert world.listings <= len(_factory_base.SLOTS)
    assert board.items["you/repo#2"]["status"] == "In flight"
    assert board.items["you/repo#3"]["status"] == "In flight"
    assert board.items["you/repo#1"]["status"] == "Ready"  # ended outside the window
    assert board.items["you/repo#4"]["status"] == "Ready"


async def test_a_tick_adds_the_item_of_a_live_run_that_is_not_on_the_board(world, monkeypatch):
    from flyte_agent_factory import _factory_base, _reconcile

    slot = _factory_base.SLOTS[0]
    labels = lambda ref: {_reconcile.LABEL_ISSUE: _issues.issue_label(ref)}  # noqa: E731 -- a one-line fake, clearer inline
    world.add("live", slot, labels=labels("you/repo#2"))
    world.add("gone", slot, phase="SUCCEEDED", labels=labels("you/repo#3"))
    ended(world.runs["gone"], 5)
    world.runs["live"].report = _dashboard.status_script(snapshot(branch="factory/a"))
    board = Board({"you/repo#1": {"status": "Ready"}}).install(monkeypatch)
    board.issues.update({"you/repo#2", "you/repo#3"})
    monkeypatch.setattr(_reconcile, "PROFILE", board_profile())
    monkeypatch.setattr(_board, "take_token", lambda *a: "token")
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda *a: "")
    # The launch request is in storage the fake world has none of; it links each run to its issue.
    linked = {labels(ref)[_reconcile.LABEL_ISSUE]: ref for ref in ("you/repo#2", "you/repo#3")}
    monkeypatch.setattr(_reconcile, "_run_issue", lambda profile, lbl: linked[lbl[_reconcile.LABEL_ISSUE]])
    await _reconcile.reconcile()
    assert board.added == ["you/repo#2"]  # not the ended run's issue
    assert board.items["you/repo#2"]["status"] == "In flight"


def test_heal_milestones_uses_the_runs_it_is_given(world):
    from flyte_agent_factory import _reconcile

    world.listings = 0
    assert _reconcile.heal_milestones(world.profile, github(App()), runs=[]) == 0
    assert world.listings == 0


# --- the reconciler only moves a live run's item forward --------------------------------------- #

PR = "https://github.com/x/y/pull/3"


@pytest.mark.parametrize(
    "current, data",
    [
        ("In review", snapshot()),  # the report lags the supervisor's move to In review
        ("In review", snapshot(branch="factory/a")),
        ("Done", snapshot()),  # an owner's Done on a live run
        ("Done", snapshot(pr=PR)),
        ("In flight", None),
    ],
)
def test_the_reconciler_never_moves_a_live_runs_item_backward(monkeypatch, current, data):
    board = Board({ISSUE: {"status": current, "texts": {"Flyte": URL}}}).install(monkeypatch)
    _board.Reconciler(board_profile(), "token", runs({ISSUE: (True, URL, data)})).tick()
    assert board.items[ISSUE]["status"] == current
    assert not [w for w in board.writes if w[1] == "Status"]


def test_the_reconciler_still_moves_forward_and_writes_text_fields_of_a_lagging_item(monkeypatch):
    board = Board({ISSUE: {"status": "In review", "texts": {"Flyte": URL}}}).install(monkeypatch)
    lagging = snapshot(branch="factory/a")
    _board.Reconciler(board_profile(), "token", runs({ISSUE: (True, URL, lagging)})).tick()
    assert board.items[ISSUE]["texts"]["Branch"] == "factory/a"
    assert board.items[ISSUE]["status"] == "In review"
    done = snapshot(verdicts=[{"round": 1, "verdict": "PASS"}])
    _board.Reconciler(board_profile(), "token", runs({ISSUE: (True, URL, done)})).tick()
    assert board.items[ISSUE]["status"] == "Done"


def test_a_new_run_for_the_same_issue_starts_the_item_over(monkeypatch):
    other = "https://console.example/runs/r0"
    board = Board({ISSUE: {"status": "Done", "texts": {"Flyte": other}}}).install(monkeypatch)
    _board.Reconciler(board_profile(), "token", runs({ISSUE: (True, URL, snapshot())})).tick()
    assert board.items[ISSUE]["status"] == "In flight"
    assert board.items[ISSUE]["texts"]["Flyte"] == URL


def test_a_review_round_after_a_pass_preserves_the_same_runs_done_item(monkeypatch):
    board = Board({ISSUE: {"status": "Done", "texts": {"Flyte": URL}}}).install(monkeypatch)
    data = snapshot(pr=PR, verdicts=[{"round": 1, "verdict": "PASS", "at": 10}])
    data["review"].update(round=2, started_at=20)
    _board.Reconciler(board_profile(), "token", runs({ISSUE: (True, URL, data)})).tick()
    assert board.items[ISSUE]["status"] == "Done"
    assert not [w for w in board.writes if w[1] == "Status"]


def test_a_lagging_report_does_not_make_the_board_flap(monkeypatch):
    board = Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    table = {ISSUE: (True, URL, snapshot())}
    reconciler = _board.Reconciler(board_profile(), "token", runs(table))
    reconciler.tick()  # In flight
    worker = _board.supervisor(board_profile(), "token", ISSUE, lambda *row: None)
    assert worker is not None
    worker.move("In review", Flyte=URL)  # the supervisor opens the PR; the report has not caught up
    reconciler.tick()
    table[ISSUE] = (True, URL, snapshot(pr=PR))
    reconciler.tick()
    assert [v for _, f, v in board.writes if f == "Status"] == ["In flight", "In review"]


@pytest.mark.parametrize("supervisor", [False, True])
def test_new_run_status_503_keeps_old_flyte_until_retry(monkeypatch, supervisor):
    old_url = "https://console.example/runs/r0"
    board = Board({ISSUE: {"status": "Done", "texts": {"Flyte": old_url}}})
    handle = board.handle
    failing = True

    def flaky(request):
        body = json.loads(request.content)
        if failing and "SetField" in body["query"] and body["variables"]["input"]["fieldId"] == "Status":
            return httpx.Response(503)
        return handle(request)

    monkeypatch.setattr(board, "handle", flaky)
    board.install(monkeypatch)
    worker = _board.supervisor(board_profile(), "token", ISSUE, lambda *row: None)
    assert worker is not None
    reconciler = _board.Reconciler(board_profile(), "token", runs({ISSUE: (True, URL, snapshot())}))
    if supervisor:
        worker.move("In flight", Flyte=URL)
    else:
        with pytest.raises(_board.BoardError):
            reconciler.tick()
    assert board.items[ISSUE]["texts"]["Flyte"] == old_url
    failing = False
    reconciler.tick()
    reconciler.tick()
    assert board.items[ISSUE]["status"] == "In flight"
    assert board.items[ISSUE]["texts"]["Flyte"] == URL


@pytest.mark.parametrize("live", [True, False])
def test_relevant_run_survives_multiple_pages_of_newer_slot_runs(world, live):
    from flyte_agent_factory import _factory_base, _reconcile

    slot = _factory_base.SLOTS[0]
    run = world.add("relevant", slot, labels={_reconcile.LABEL_ISSUE: _issues.issue_label(ISSUE)})
    if not live:
        run.phase = "SUCCEEDED"
        ended(run, 0)
        run.report = _dashboard.status_script(snapshot(phase="released"))
    for n in range(150):
        world.add(
            f"noise-{n}",
            slot,
            phase="SUCCEEDED",
            labels={_reconcile.LABEL_ISSUE: _issues.issue_label(f"x/y#{n + 1}")},
        )
    found = _reconcile.latest_run(ISSUE)
    assert found is not None
    assert found[0] is live
    assert found[1].endswith("/relevant")


def test_run_listing_cap_warns(world, monkeypatch, caplog):
    from flyte_agent_factory import _factory_base, _reconcile

    monkeypatch.setattr(_reconcile, "RUN_PAGE_CAP", 1)
    for n in range(101):
        world.add(
            f"ended-{n}",
            _factory_base.SLOTS[0],
            phase="SUCCEEDED",
            labels={_reconcile.LABEL_ISSUE: _issues.issue_label(f"x/y#{n + 1}")},
        )
    _reconcile.project_runs()
    assert "cap" in caplog.text.lower()


async def test_board_shown_run_is_fetched_when_listing_misses_it(world, monkeypatch):
    from flyte_agent_factory import _factory_base, _reconcile

    run = world.add(
        "shown", _factory_base.SLOTS[0], labels={_reconcile.LABEL_ISSUE: _issues.issue_label(ISSUE)}
    )
    run.report = _dashboard.status_script(snapshot(phase="released"))
    url = _board.run_url(board_profile(), run.name)
    board = Board({ISSUE: {"status": "In review", "texts": {"Flyte": url}}}).install(monkeypatch)
    monkeypatch.setattr(_reconcile, "project_runs", list)
    monkeypatch.setattr(_reconcile, "PROFILE", board_profile())
    monkeypatch.setattr(_board, "take_token", lambda *a: "token")
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda *a: "")
    await _reconcile.reconcile()
    assert board.items[ISSUE]["status"] == "Done"


@pytest.mark.parametrize("live", [True, False])
def test_legacy_pass_snapshot_is_done(live):
    # A pass with a pull request is In review until it merges (the merge or the issue closing makes it Done).
    data = snapshot(phase="done", pr="")
    assert _board.desired(live, URL, data)["Status"] == "Done"
    data["phase"]["kind"] = "building"
    data["review"]["passed"] = True
    assert _board.desired(live, URL, data)["Status"] == "Done"
    data["deliverables"]["pr"] = PR
    assert _board.desired(live, URL, data)["Status"] == "In review"


def test_direct_lookup_without_sdk_run_labels_uses_board_issue(world):
    from flyte_agent_factory import _factory_base, _reconcile

    run = world.add("shown-without-labels", _factory_base.SLOTS[0])
    run.report = _dashboard.status_script(snapshot(phase="released"))
    url = _board.run_url(_reconcile.PROFILE, run.name)
    index = _reconcile.RunIndex([])
    index.include_shown(ISSUE, url)
    latest = index.latest(ISSUE)
    assert latest is not None
    assert latest[1] == url
    assert index.latest("x/y#99") is None


async def test_dropped_webhook_event_is_healed_by_one_hourly_tick(world, monkeypatch):
    from flyte_agent_factory import _factory_base, _reconcile

    board = Board({ISSUE: {"status": "In flight"}}).install(monkeypatch)
    run = world.add(
        "missed-completion",
        _factory_base.SLOTS[0],
        labels={_reconcile.LABEL_ISSUE: _issues.issue_label(ISSUE)},
    )
    run.report = _dashboard.status_script(snapshot(phase="released"))
    # Drop the PR-status webhook entirely: do not post to /github. The supervisor's last
    # board write also missed, so only the safety-net task can repair this item.
    run.phase = "SUCCEEDED"
    ended(run, 1)
    monkeypatch.setattr(_reconcile, "PROFILE", board_profile())
    monkeypatch.setattr(_board, "take_token", lambda *a: "token")
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda *a: "")
    assert board.items[ISSUE]["status"] == "In flight"
    automation = _reconcile.reconcile.triggers[0].automation
    assert isinstance(automation, flyte.FixedRate) and automation.interval_minutes == 60
    await _reconcile.reconcile()
    assert board.items[ISSUE]["status"] == "Done"


async def test_completion_repair_waits_until_the_sender_is_terminal(world, monkeypatch):
    from flyte_agent_factory import _factory_base, _reconcile

    board = Board({ISSUE: {"status": "In flight"}}).install(monkeypatch)
    run = world.add(
        "finishing", _factory_base.SLOTS[0], labels={_reconcile.LABEL_ISSUE: _issues.issue_label(ISSUE)}
    )
    run.report = _dashboard.status_script(snapshot(phase="released"))
    polls = []

    async def sleep(seconds):
        assert seconds == 2
        assert board.writes == []
        polls.append(run.phase)
        run.phase = "SUCCEEDED"
        ended(run, 0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(_reconcile, "PROFILE", board_profile())
    monkeypatch.setattr(_board, "take_token", lambda *a: "token")
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda *a: "")
    real_get = inspect.signature(flyte.remote.Run.get)

    def get(*args, **kwargs):
        # Bind against the SDK's real signature: a `**kwargs` stub hid that Run.get takes only `name`,
        # so every completion-triggered reconcile failed with a TypeError in production.
        real_get.bind(*args, **kwargs)
        return run

    monkeypatch.setattr(flyte.remote.Run, "get", get)
    await _reconcile.reconcile(completion_run=run.name)
    assert polls == ["RUNNING"]
    assert board.items[ISSUE]["status"] == "Done"


def test_a_completion_written_after_the_listing_is_not_undone_by_the_reconciler(monkeypatch, no_storage):
    # QA #55 round 4: the listing showed Backlog; a merge closed the issue and wrote Done before the
    # reconciler projected the live run's In flight. The write must use the card as it is now.
    board = Board({ISSUE: {"status": "Backlog"}}).install(monkeypatch)
    latest = runs({ISSUE: (True, URL, snapshot(branch="factory/a"))})

    def completed_meanwhile(ref):
        board.items[ISSUE]["status"] = "Done"
        board.closed = {*getattr(board, "closed", set()), ISSUE}
        return latest(ref)

    monkeypatch.setattr(board, "issue_state", lambda ref: "CLOSED" if ref == ISSUE else "OPEN", raising=False)
    _board.Reconciler(board_profile(), "token", completed_meanwhile).tick()
    assert board.items[ISSUE]["status"] == "Done"
    assert (ISSUE, "Status", "In flight") not in board.writes


# --- the truth pass runs from the periodic task ----------------------------------------------------- #


async def test_reconcile_runs_the_truth_pass_after_the_tick_and_reports_its_corrections(world, monkeypatch):
    from flyte_agent_factory import _board_events, _github, _reconcile

    calls: list[str] = []
    seen: dict[str, object] = {}

    def drift(profile, token, app, latest, *, newest, queued):
        calls.append("drift")
        seen.update(token=token, queued=queued, newest=newest(ISSUE))
        return [f"{ISSUE}: Done -> In review: open pull request", f"{ISSUE}: Ready -> Done: issue is closed"]

    monkeypatch.setattr(_board_events, "drift", drift)
    monkeypatch.setattr(_reconcile, "heal_milestones", lambda *args: 0)
    monkeypatch.setattr(_reconcile, "queued_issues", lambda profile: {ISSUE.lower()})
    monkeypatch.setattr(_github, "GithubApp", lambda *args: object())
    Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    monkeypatch.setattr(_reconcile, "PROFILE", board_profile(app=True))
    monkeypatch.setattr(_board, "take_token", lambda *a: "token")
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda *a: "key")
    result = await _reconcile.reconcile()
    assert calls == ["drift"]  # one reconciler: no second board pass
    assert seen == {"token": "token", "queued": {ISSUE.lower()}, "newest": None}
    assert "2 board truth correction(s)" in result


async def test_reconcile_without_the_app_skips_the_truth_pass(world, monkeypatch, caplog):
    from flyte_agent_factory import _board_events, _reconcile

    monkeypatch.setattr(_board_events, "drift", lambda *args, **kwargs: pytest.fail("needs the App"))
    Board({ISSUE: {"status": "Ready"}}).install(monkeypatch)
    monkeypatch.setattr(_reconcile, "PROFILE", board_profile())
    monkeypatch.setattr(_board, "take_token", lambda *a: "token")
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda *a: "")
    with caplog.at_level("INFO"):
        assert await _reconcile.reconcile() == "board"
    assert "Board truth skipped" in caplog.text


def test_newest_run_ignores_the_board_window_and_queued_issues_need_state(world):
    from flyte_agent_factory import _factory_base, _reconcile

    labels = {_reconcile.LABEL_ISSUE: _issues.issue_label(ISSUE)}
    run = world.add("old", _factory_base.SLOTS[0], phase="SUCCEEDED", labels=labels)
    run.report = _dashboard.status_script(snapshot(phase="failed"))
    ended(run, 30)
    index = _reconcile.RunIndex([run])
    assert index.latest(ISSUE) is None  # past BOARD_WINDOW: not the tick's to project
    newest = index.newest(ISSUE)
    assert newest is not None
    live, at, data = newest
    assert not live and at is not None and data is not None and data["phase"]["kind"] == "failed"
    assert index.newest("x/y#99") is None
    assert _reconcile.queued_issues(dataclasses.replace(board_profile(), state=_profile.State())) == set()


def test_queued_issues_are_the_pending_requests_issue_references(world):
    from flyte_agent_factory import _queue, _reconcile

    for name, issue in (("a", "Example/Scratch#8"), ("b", ""), ("c", "nowhere#3")):
        request = _queue.Request(name, 1, "new", "b", "opus", "r", "", {}, 1.0, issue=issue)
        _queue.enqueue(world.queue, request)
    profile = dataclasses.replace(board_profile(), state=world.profile.state)
    assert _reconcile.queued_issues(profile) == {"example/scratch#8"}
