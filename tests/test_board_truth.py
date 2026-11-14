"""The board's Status follows live facts, corrected by `_board_events.drift`: the 2026-11-12 drift cases,
upstream PRs, the hand-set guard."""

import dataclasses
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from flyte_agent_factory import _board, _board_events, _github
from flyte_agent_factory._board_events import Facts, derive
from tests.test_board_events import REPO, App
from tests.test_board_events import world as world
from tests.test_reconcile import Board, board_profile, snapshot

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=10)).isoformat()
RECENT = (NOW - timedelta(hours=30)).isoformat()  # past the hand-set guard, not yet stale


def ref(number: int) -> str:
    return f"{REPO}#{number}"


class TruthBoard(Board):
    """The shared board fake, answering what the truth pass adds: who set Status when, PR cards, upstream PRs.

    An item may carry `set_at` (ISO 8601), `set_by` (a GraphQL typename), `type`, `body` and `linked`.
    """

    def __init__(self, items: dict[str, dict[str, Any]] | None = None):
        super().__init__({r: {"set_at": OLD, "set_by": "Bot", **item} for r, item in (items or {}).items()})
        self.upstream: dict[str, tuple[str, bool]] = {}  # "o/r#N" -> (state, merged)
        self.mutations: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "UpstreamPull" in body["query"]:
            variables = body["variables"]
            state = self.upstream.get(f"{variables['owner']}/{variables['name']}#{variables['number']}")
            pull = {"state": state[0], "merged": state[1]} if state else None
            return httpx.Response(200, json={"data": {"repository": {"pullRequest": pull}}})
        if "UpdateField" in body["query"]:
            self.mutations.append(body["variables"]["input"])
        response = super().handle(request)
        data = response.json()["data"]
        nodes = (data.get("node") or {}).get("items", {}).get("nodes", [data.get("node")])
        for node in filter(None, nodes):
            item = self.items[self._ref(node["id"])]
            status = node.get("fieldValueByName")
            if status:
                status.update(updatedAt=item["set_at"], creator={"__typename": item["set_by"], "login": "x"})
            content = node.get("content") or {}
            if "number" in content:
                content["__typename"] = "PullRequest" if item.get("type") == "pr" else "Issue"
                content["body"] = item.get("body", "")
                content["closedByPullRequestsReferences"] = {
                    "nodes": [{"state": "OPEN", "number": 1}] if item.get("linked") else []
                }
        return httpx.Response(200, json={"data": data})


@pytest.fixture
def truth_world(monkeypatch):
    profile = dataclasses.replace(board_profile(app=True), repo=f"https://github.com/{REPO}.git")
    board = TruthBoard().install(monkeypatch)
    app = App()
    board.issue_state = lambda r: app.issues.get(int(r.split("#")[1]), "open").upper()
    return profile, board, app


def run_truth(world, *, newest=lambda r: None, queued=(), now=NOW):
    profile, board, app = world
    rows = _board_events.drift(profile, "token", app, lambda r: None, newest=newest, queued=queued, now=now)
    return [row for row in rows if _board_events.is_correction(row)]


def card(board, number, status, **item):
    board.items[ref(number)] = {"status": status, "texts": {}, "set_at": OLD, "set_by": "Bot", **item}


def pull(app, number, text=None):
    app.pulls.append({"title": text or f"Closes #{number}", "body": ""})


# --- derive: one rule at a time ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "facts, current, expected",
    [
        # 1: pull-request cards
        (Facts(pr_card=True), "Backlog", "In review"),
        (Facts(pr_card=True), "Ready", "In review"),
        (Facts(pr_card=True), "In flight", None),
        (Facts(pr_card=True, issue_open=False), "Backlog", None),
        # 2: a closed issue is Done, and stays Done
        (Facts(issue_open=False), "In review", "Done"),
        (Facts(issue_open=False, open_pr=True, live=True), "Backlog", "Done"),
        (Facts(issue_open=False), "Done", None),
        # 3: upstream merged
        (Facts(upstream=(("o/r#1", "MERGED"),)), "Upstream", "Done"),
        (Facts(upstream=(("o/r#1", "MERGED"), ("o/r#2", "OPEN"))), "In review", "Upstream"),
        (Facts(upstream=(("o/r#1", "MERGED"),), open_pr=True), "In review", "Done"),
        # 4: merged on main is drift's to flag
        (Facts(merged=True, open_pr=True), "Done", None),
        # 5: open local PR, review round
        (Facts(open_pr=True), "Done", "In review"),
        (Facts(open_pr=True), "Upstream", "In review"),
        (Facts(open_pr=True), "In flight", "In review"),
        (Facts(open_pr=True), "In review", None),
        (Facts(live=True, review_live=True), "In flight", "In review"),
        # 6: a live run moves only a card that is behind
        (Facts(live=True), "Ready", "In flight"),
        (Facts(live=True), "In review", None),
        (Facts(live=True), "Upstream", None),
        # 7: upstream open
        (Facts(upstream=(("o/r#1", "OPEN"),)), "In review", "Upstream"),
        (Facts(upstream=(("o/r#1", "OPEN"),)), "Done", "Upstream"),
        (Facts(upstream=(("o/r#1", "OPEN"),)), "Upstream", None),
        (Facts(upstream=(("o/r#1", "OPEN"),), live=True), "In review", None),
        # 8: upstream closed unmerged
        (Facts(upstream=(("o/r#1", "CLOSED"),)), "Upstream", "Ready"),
        (Facts(upstream=(("o/r#1", "CLOSED"),)), "Ready", None),
        (Facts(upstream=(("o/r#1", "CLOSED"),)), "Backlog", None),
        # a closed PR resubmitted and merged is Done (nothing open, one merged)
        (Facts(upstream=(("o/r#1", "CLOSED"), ("o/r#2", "MERGED"))), "Upstream", "Done"),
        (Facts(upstream=(("o/r#1", "CLOSED"), ("o/r#2", "MERGED"))), "Ready", "Done"),
        # 9: Done but open, the run stopped undelivered
        (Facts(stopped=True), "Done", "Ready"),
        (Facts(), "Done", None),
        (Facts(stopped=True, active_at=NOW), "In review", None),
        # 10: nothing alive for two days
        (Facts(active_at=NOW - timedelta(days=3)), "In review", "Ready"),
        (Facts(active_at=NOW - timedelta(days=3)), "In flight", "Ready"),
        (Facts(active_at=NOW - timedelta(days=1)), "In flight", None),
        (Facts(active_at=NOW - timedelta(days=3), queued=True), "In flight", None),
        (Facts(active_at=NOW - timedelta(days=3)), "Upstream", None),
        (Facts(), "In flight", "Ready"),
        # 11: queued
        (Facts(queued=True), "Backlog", "Ready"),
        (Facts(queued=True), "Ready", None),
        (Facts(), "Backlog", None),
    ],
)
def test_derive(facts, current, expected):
    status, reason = derive(facts, current, NOW)
    assert status == expected
    assert bool(reason) == (expected is not None)


def test_derive_never_moves_done_when_the_issue_is_closed():
    for facts in (
        Facts(issue_open=False, stopped=True),
        Facts(issue_open=False, upstream=(("o/r#1", "OPEN"),)),
    ):
        assert derive(facts, "Done", NOW) == (None, "")


# --- the 2026-11-12 drift cases ------------------------------------------------------------------ #


def test_done_with_open_pr_moves_to_in_review(truth_world):  # #194
    _, board, app = truth_world
    card(board, 194, "Done")
    pull(app, 194)
    rows = run_truth(truth_world)
    assert board.items[ref(194)]["status"] == "In review"
    assert rows == [f"{ref(194)}: Done -> In review: open pull request"]


def test_open_pr_cards_in_backlog_move_to_in_review(truth_world):  # #198-#200
    _, board, _ = truth_world
    for number in (198, 199):
        card(board, number, "Backlog", type="pr")
    card(board, 200, "Backlog", linked=True)  # an issue with an open linked PR
    card(board, 201, "Ready", type="pr")
    card(board, 202, "Backlog", type="pr")
    board.issue_state = lambda r: "CLOSED" if r.endswith("#202") else "OPEN"  # a closed PR card
    run_truth(truth_world)
    assert [board.items[ref(n)]["status"] for n in (198, 199, 200, 201)] == ["In review"] * 4
    assert board.items[ref(202)]["status"] == "Backlog"


def test_done_after_undelivered_stop_moves_to_ready(truth_world):  # #175
    _, board, _ = truth_world
    card(board, 175, "Done")
    card(board, 176, "Done")
    ended = NOW - timedelta(days=4)
    runs = {
        ref(175): (False, ended, snapshot(phase="failed")),
        ref(176): (False, ended, snapshot(phase="released")),  # delivered: stays Done
    }
    rows = run_truth(truth_world, newest=runs.get)
    assert board.items[ref(175)]["status"] == "Ready"
    assert board.items[ref(176)]["status"] == "Done"
    assert rows == [f"{ref(175)}: Done -> Ready: run stopped without delivering"]


def test_done_after_a_pass_whose_pr_merged_without_closing_the_issue_stays_done(truth_world):
    _, board, _ = truth_world
    card(board, 177, "Done")
    passed = snapshot(phase="done", pr="https://github.com/o/r/pull/9")  # desired(): In review
    rows = run_truth(truth_world, newest=lambda r: (False, NOW - timedelta(days=4), passed))
    assert board.items[ref(177)]["status"] == "Done"
    assert rows == []


def test_done_with_an_unreadable_report_stays_done(truth_world):
    _, board, _ = truth_world
    card(board, 175, "Done")
    run_truth(truth_world, newest=lambda r: (False, NOW - timedelta(days=4), None))
    assert board.items[ref(175)]["status"] == "Done"


def test_stale_in_review_and_in_flight_move_to_ready(truth_world):  # #166/#82/#139
    _, board, _ = truth_world
    card(board, 166, "In review")
    card(board, 82, "In flight")
    card(board, 139, "In flight", set_at=(NOW - timedelta(days=1)).isoformat())  # moved a day ago
    run_truth(truth_world, newest=lambda r: (False, NOW - timedelta(days=5), snapshot(phase="failed")))
    assert [board.items[ref(n)]["status"] for n in (166, 82, 139)] == ["Ready", "Ready", "In flight"]


def test_stale_cards_with_a_live_run_or_open_pr_are_left_alone(truth_world):
    _, board, app = truth_world
    card(board, 1, "In flight")
    card(board, 2, "In review")
    card(board, 3, "In flight", texts={"Branch": "factory/x"})
    pull(app, 2)
    app.branches = [
        {
            "name": "factory/x",
            "commit": {"committer": {"date": (NOW - timedelta(hours=3)).isoformat()}, "message": "wip"},
        }
    ]
    live = {ref(1): (True, None, snapshot())}
    assert run_truth(truth_world, newest=live.get) == []
    assert [board.items[ref(n)]["status"] for n in (1, 2, 3)] == ["In flight", "In review", "In flight"]


def test_queued_backlog_moves_to_ready(truth_world):
    _, board, _ = truth_world
    card(board, 7, "Backlog")
    card(board, 8, "Backlog")
    run_truth(truth_world, queued={ref(7)})
    assert [board.items[ref(n)]["status"] for n in (7, 8)] == ["Ready", "Backlog"]


def test_a_closed_issue_not_done_becomes_done(truth_world):
    _, board, app = truth_world
    card(board, 5, "In flight")
    app.issues[5] = "closed"
    run_truth(truth_world)
    assert board.items[ref(5)]["status"] == "Done"


# --- one reconciler: shared facts, no double reporting ------------------------------------------- #


def test_a_tick_reads_pulls_commits_and_issues_once(truth_world):
    _, board, app = truth_world
    for number in (1, 2, 3):
        card(board, number, "In flight")
    calls: list[str] = []
    for name in ("repo_pulls", "main_commits", "repo_issues"):
        real = getattr(app, name)

        def counted(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        setattr(app, name, counted)
    run_truth(truth_world)
    assert sorted(calls) == ["main_commits", "repo_issues", "repo_pulls"]


def test_a_card_corrected_by_truth_is_not_also_flagged(truth_world):
    profile, board, app = truth_world
    card(board, 194, "Done")  # Done but issue open, with an open PR: corrected to In review
    card(board, 166, "In review")  # no PR, stale: corrected to Ready, not "In review without open PR"
    card(board, 82, "In flight")  # stale: corrected, not "no activity for 7 days"
    card(board, 9, "Done")  # nothing to correct: still flagged
    pull(app, 194)
    rows = _board_events.drift(profile, "token", app, lambda r: None, newest=lambda r: None, now=NOW)
    assert rows == [
        f"{ref(194)}: Done -> In review: open pull request",
        f"{ref(166)}: In review -> Ready: no live run, pull request or activity for 2 days",
        f"{ref(82)}: In flight -> Ready: no live run, pull request or activity for 2 days",
        f"{ref(9)}: Done but issue open; needs review",
    ]


def test_a_held_correction_is_still_flagged(truth_world):
    profile, board, app = truth_world
    card(board, 194, "Done", set_at=(NOW - timedelta(hours=3)).isoformat(), set_by="User")
    pull(app, 194)
    rows = _board_events.drift(profile, "token", app, lambda r: None, newest=lambda r: None, now=NOW)
    assert rows == [f"{ref(194)}: Done but issue open; needs review"]
    assert board.items[ref(194)]["status"] == "Done"


# --- upstream pull requests ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Upstream: https://github.com/acme/lib/pull/12", ["acme/lib#12"]),
        ("upstream: Acme/Lib#12", ["acme/lib#12"]),
        ("UPSTREAM: <https://github.com/acme/lib/pull/12>", ["acme/lib#12"]),
        ("Upstream: acme/lib#1\nupstream: https://github.com/acme/lib/pull/2", ["acme/lib#1", "acme/lib#2"]),
        ("Upstream: acme/lib#1 and acme/lib#1, other/x#3", ["acme/lib#1", "other/x#3"]),
        ("> Upstream: acme/lib#1\n  > Upstream: acme/lib#2", []),
        ("see Upstream: acme/lib#1", []),
        ("Upstream: example/scratch#4\nUpstream: https://github.com/example/scratch/pull/5", []),
        ("Upstream: #4", []),
    ],
)
def test_upstream_link_parser(text, expected):
    assert _board_events.upstream_links(text, {"example/scratch"}) == expected


def test_the_factorys_own_repositories_are_not_upstream(truth_world):
    profile, _, _ = truth_world
    repos = {
        "project": "git@github.com:acme/Work.git",
        "tools": "https://github.com/acme/tools",
        "x": "ssh://other",
    }
    profile = dataclasses.replace(profile, repos=repos, repo="git@github.com:acme/Work.git")
    assert _board_events._own_repos(profile) == {"acme/work", "acme/tools"}


@pytest.mark.parametrize(
    "state, status, merged",
    [("OPEN", "Upstream", False), ("MERGED", "Done", True), ("CLOSED", "Ready", False)],
)
def test_upstream_pull_request_state_moves_the_card(truth_world, state, status, merged):
    _, board, app = truth_world
    card(board, 9, "In review", body="Handed over.\nUpstream: acme/lib#7")
    board.upstream["acme/lib#7"] = (state, merged)
    run_truth(truth_world)
    assert board.items[ref(9)]["status"] == status


def test_an_open_upstream_pr_moves_the_item_to_upstream(truth_world):
    _, board, _ = truth_world
    card(board, 9, "In review", body="Handed over.\nUpstream: acme/lib#7")
    board.upstream["acme/lib#7"] = ("OPEN", False)
    run_truth(truth_world)
    assert board.items[ref(9)]["status"] == "Upstream"


def test_an_upstream_merge_moves_the_item_to_done(truth_world):
    _, board, _ = truth_world
    card(board, 9, "Upstream", body="Upstream: acme/lib#7")
    board.upstream["acme/lib#7"] = ("MERGED", True)
    run_truth(truth_world)
    assert board.items[ref(9)]["status"] == "Done"


def test_an_upstream_close_without_merge_moves_the_item_back(truth_world):
    _, board, _ = truth_world
    card(board, 9, "Upstream", body="Upstream: acme/lib#7")
    board.upstream["acme/lib#7"] = ("CLOSED", False)
    run_truth(truth_world)
    assert board.items[ref(9)]["status"] == "Ready"


def test_upstream_link_in_a_comment_counts(truth_world):
    _, board, app = truth_world
    card(board, 9, "In review")
    app.posted.append((9, "Upstream: https://github.com/acme/lib/pull/7"))
    board.upstream["acme/lib#7"] = ("OPEN", False)
    run_truth(truth_world)
    assert board.items[ref(9)]["status"] == "Upstream"


def test_open_local_pr_wins_over_an_open_upstream_pr(truth_world):
    _, board, app = truth_world
    card(board, 9, "Upstream", body="Upstream: acme/lib#7")
    board.upstream["acme/lib#7"] = ("OPEN", False)
    pull(app, 9)
    run_truth(truth_world)
    assert board.items[ref(9)]["status"] == "In review"


def test_upstream_closed_unmerged_posts_one_note_per_pull_request(truth_world):
    _, board, app = truth_world
    card(board, 9, "Upstream", body="Upstream: acme/lib#7")
    board.upstream["acme/lib#7"] = ("CLOSED", False)
    run_truth(truth_world)
    assert board.items[ref(9)]["status"] == "Ready"
    notes = [text for number, text in app.posted if number == 9]
    assert len(notes) == 1 and "acme/lib#7" in notes[0] and "closed without merging" in notes[0]
    # The card went back to Upstream (by hand, long ago): the same closed PR posts nothing again.
    board.items[ref(9)]["status"] = "Upstream"
    run_truth(truth_world)
    assert board.items[ref(9)]["status"] == "Ready"
    assert len([1 for number, _ in app.posted if number == 9]) == 1


@pytest.mark.parametrize("status", ["Backlog", "Ready"])
@pytest.mark.parametrize(
    "state, merged, expected",
    [("OPEN", False, "Upstream"), ("MERGED", True, "Done"), ("CLOSED", False, None)],
)
def test_backlog_and_ready_cards_are_read_for_upstream_links(truth_world, status, state, merged, expected):
    _, board, app = truth_world
    card(board, 9, status, body="Upstream: acme/lib#7")
    board.upstream["acme/lib#7"] = (state, merged)
    run_truth(truth_world)
    assert board.items[ref(9)]["status"] == (expected or status)
    assert [text for number, text in app.posted if number == 9] == []  # closed unmerged: no note either


# --- the hand-set guard -------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "hours, by, moved", [(23, "User", False), (25, "User", True), (1, "Bot", True), (23, "", True)]
)
def test_hand_set_guard(truth_world, caplog, hours, by, moved):
    _, board, app = truth_world
    card(board, 194, "Done", set_at=(NOW - timedelta(hours=hours)).isoformat(), set_by=by)
    pull(app, 194)
    with caplog.at_level(logging.WARNING):
        rows = run_truth(truth_world)
    assert (board.items[ref(194)]["status"] == "In review") == moved
    assert bool(rows) == moved
    held = (
        f"Board truth {ref(194)}: held Done (set by hand {hours}h ago); "
        "would move to In review: open pull request"
    )
    assert (held in caplog.text) == (not moved)


def test_a_correction_is_logged_as_one_warning(truth_world, caplog):
    _, board, app = truth_world
    card(board, 194, "Done")
    pull(app, 194)
    with caplog.at_level(logging.WARNING):
        run_truth(truth_world)
    lines = [r.getMessage() for r in caplog.records if r.name == _board_events.log.name]
    assert lines == [f"Board truth {ref(194)}: Done -> In review: open pull request"]
    assert caplog.records[0].levelno == logging.WARNING


def test_a_card_that_changed_since_the_listing_is_decided_again(truth_world, monkeypatch):
    _, board, app = truth_world
    card(board, 194, "Done")
    pull(app, 194)
    real = _board.current_item

    def moved_since(client, item):
        board.items[ref(194)]["status"] = "In review"  # another writer got there first
        return real(client, item)

    monkeypatch.setattr(_board, "current_item", moved_since)
    assert run_truth(truth_world) == []
    assert board.writes == []


# --- errors -------------------------------------------------------------------------------------- #


def test_one_cards_failure_does_not_stop_the_others_and_is_raised_at_the_end(truth_world):
    _, board, app = truth_world
    card(board, 82, "In review")
    card(board, 83, "In flight")
    pull(app, 83, "Closes #83")
    real = app.issue_comments_since

    def comments(repo, number, since=""):
        if number == 82:
            raise _github.GithubError("github: HTTP 502", 502)
        return real(repo, number, since)

    app.issue_comments_since = comments
    with pytest.raises(_github.GithubError):
        run_truth(truth_world)
    assert board.items[ref(83)]["status"] == "In review"
    assert board.items[ref(82)]["status"] == "In review"


# --- the Upstream column ------------------------------------------------------------------------- #


def status_field(options):
    return {"id": "status", "name": "Status", "options": options}


def bootstrap_input(existing):
    updates: list[dict[str, Any]] = []

    def handle(request):
        body = json.loads(request.content)
        query, variables = body["query"], body["variables"]
        if "ResolveProject" in query:
            nodes = [existing] + [{"id": n, "name": n, "dataType": "TEXT"} for n in _board.TEXT_FIELDS]
            nodes += [
                {"id": n, "name": n, "options": [{"id": f"{n}-{o}", "name": o} for o in opts]}
                for n, opts in _board.SELECTS.items()
                if n != "Status"
            ]
            data: dict[str, Any] = {
                "user": {
                    "projectV2": {"id": "p", "fields": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}
                }
            }
        else:
            updates.append(variables["input"])
            options = [
                dict(o, id=o.get("id", f"new-{o['name']}")) for o in variables["input"]["singleSelectOptions"]
            ]
            data = {"updateProjectV2Field": {"projectV2Field": dict(existing, options=options)}}
        return httpx.Response(200, json={"data": data})

    board = _board.BoardClient("test", transport=httpx.MockTransport(handle))
    board.bootstrap("example", "user", 1)
    return board, updates


def test_bootstrap_inserts_upstream_after_in_review_and_keeps_every_option():
    options = [
        {"id": "a", "name": "Backlog", "color": "GRAY", "description": "mine"},
        {"id": "b", "name": "Ready", "color": "GREEN", "description": ""},
        {"id": "x", "name": "Parked", "color": "RED", "description": "custom"},
        {"id": "c", "name": "In flight", "color": "YELLOW", "description": ""},
        {"id": "d", "name": "In review", "color": "ORANGE", "description": ""},
        {"id": "e", "name": "Done", "color": "PURPLE", "description": "shipped"},
    ]
    board, updates = bootstrap_input(status_field(options))
    assert len(updates) == 1 and updates[0]["fieldId"] == "status"
    sent = updates[0]["singleSelectOptions"]
    assert [o["name"] for o in sent] == [
        "Backlog",
        "Ready",
        "Parked",
        "In flight",
        "In review",
        "Upstream",
        "Done",
    ]
    assert [o for o in sent if o["name"] != "Upstream"] == options  # ids, colours, descriptions untouched
    assert sent[5] == {"name": "Upstream", "color": "GRAY", "description": ""}
    assert board.missing_fields() == []


def test_bootstrap_leaves_a_board_that_has_upstream_alone():
    options = [{"id": n, "name": n} for n in ("Done", "Upstream", *_board.STATUSES[:4])]  # any order
    board, updates = bootstrap_input(status_field(options))
    assert updates == []


def test_missing_fields_reports_status_without_upstream():
    board = _board.BoardClient("test")
    for name, options in _board.SELECTS.items():
        board.fields[name] = {"name": name, "options": [{"name": o} for o in options if o != "Upstream"]}
    board.fields.update({n: {"name": n, "dataType": "TEXT"} for n in _board.TEXT_FIELDS})
    assert board.missing_fields() == ["Status"]


@pytest.mark.parametrize(
    "present, expected",
    [
        (("Backlog", "Done"), ("Backlog", "Upstream", "Done")),
        (("Done",), ("Upstream", "Done")),
        (("In review",), ("In review", "Upstream")),
        ((), ("Upstream",)),
    ],
)
def test_option_insertion_with_a_sparse_board(present, expected):
    existing = [{"id": n, "name": n} for n in present]
    wanted = tuple(o for o in _board.STATUSES if o in present or o == "Upstream")
    assert tuple(o["name"] for o in _board._with_options(existing, wanted)) == expected


def test_drift_does_not_flag_the_upstream_option(world):
    profile, board, app = world
    status = next(f for f in board.fields if f["name"] == "Status")
    assert "Upstream" in [o["name"] for o in status["options"]]
    assert _board_events.drift(profile, "token", app, lambda r: None) == []


def test_upstream_is_between_in_review_and_done_for_forward_moves():
    class Writer:
        def __init__(self, status):
            self.status, self.writes = status, []

        def status_of(self, item):
            return self.status, True

        def set_status(self, item, name):
            self.writes.append(name)

    assert _board.advance_status(Writer("In review"), "i", "Upstream")
    assert not _board.advance_status(Writer("Upstream"), "i", "In review")
    assert _board.advance_status(Writer("Upstream"), "i", "Done")
    assert not _board.advance_status(Writer("Done"), "i", "Upstream")


# --- malformed upstream links -------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text", ["Upstream: my_org/lib#5", "Upstream: see example.com/x#1", "Upstream: a.b/c#2"]
)
def test_text_that_is_not_a_github_owner_is_not_an_upstream_link(text):
    assert _board_events.upstream_links(text, set()) == []


def test_a_malformed_upstream_link_does_not_stop_the_pass(truth_world):
    _, board, _ = truth_world
    card(board, 1, "In review", set_at=RECENT, body="Upstream: my_org/lib#5")
    card(board, 2, "In review", set_at=RECENT, body="Upstream: see example.com/x#1")
    card(board, 3, "In review", set_at=RECENT, body="Upstream: acme/lib#7")
    board.upstream["acme/lib#7"] = ("OPEN", False)
    run_truth(truth_world)
    assert [board.items[ref(n)]["status"] for n in (1, 2, 3)] == ["In review", "In review", "Upstream"]


def test_pull_request_state_of_an_unparsable_link_is_empty():
    assert _board.BoardClient("token").pull_request_state("my_org/lib#5") == ""
    assert _board.BoardClient("token").pull_request_state("nonsense") == ""


# --- a closed issue's Done is not held by the guard ---------------------------------------------- #


def test_a_closed_issue_goes_to_done_whoever_set_its_status(truth_world, caplog):
    _, board, app = truth_world
    card(board, 5, "In flight", set_at=(NOW - timedelta(hours=1)).isoformat(), set_by="User")
    app.issues[5] = "closed"
    with caplog.at_level(logging.WARNING):
        rows = run_truth(truth_world)
    assert board.items[ref(5)]["status"] == "Done"
    assert rows == [f"{ref(5)}: In flight -> Done: issue is closed"]
    assert "held" not in caplog.text


def test_the_heal_is_not_held_by_the_guard_either(truth_world):
    profile, board, app = truth_world
    card(board, 5, "In flight", set_at=(NOW - timedelta(hours=1)).isoformat(), set_by="User")
    app.issues[5] = "closed"
    rows = _board_events.drift(profile, "token", app, lambda r: None, now=NOW)  # no corrections pass
    assert board.items[ref(5)]["status"] == "Done"
    assert rows == [f"{ref(5)}: closed but card not Done; healed"]


# --- run projection and truth do not fight ------------------------------------------------------- #

PASSED = {"verdicts": [{"round": 1, "verdict": "PASS"}]}


def rounds(world, number, found, *, status, **item):
    """Tick the run projection then drift, twice; the writes of each round."""
    profile, board, app = world
    card(board, number, status, **item)
    url = "https://console.example/runs/r1"
    board.items[ref(number)]["texts"] = {"Flyte": url}
    out = []
    for _ in range(2):
        board.writes.clear()
        _board.Reconciler(
            profile, "token", lambda r: (False, url, found) if r == ref(number) else None
        ).tick()
        _board_events.drift(
            profile,
            "token",
            app,
            lambda r: None,
            newest=lambda r: (False, NOW - timedelta(hours=1), found),
            now=NOW,
        )
        out.append(list(board.writes))
    return out, board.items[ref(number)]["status"]


def test_a_passed_run_with_a_local_pr_and_the_board_settle_in_one_round(truth_world):
    _, _, app = truth_world
    pull(app, 9)
    found = snapshot(pr="https://github.com/x/y/pull/3", **PASSED)
    out, last = rounds(truth_world, 9, found, status="In flight")
    assert last == "In review"
    assert [v for _, field, v in out[0] if field == "Status"] == ["In review"]
    assert out[1] == []


def test_a_passed_run_without_a_pr_and_an_open_upstream_pr_settle(truth_world):
    _, board, _ = truth_world
    board.upstream["acme/lib#7"] = ("OPEN", False)
    out, last = rounds(truth_world, 9, snapshot(**PASSED), status="In flight", body="Upstream: acme/lib#7")
    assert last == "Upstream"
    assert out[1] == []


def test_a_run_done_never_overwrites_upstream(truth_world):
    profile, board, _ = truth_world
    url = "https://console.example/runs/r1"
    # A card that shows another run: `_forward_only` does not apply, `_write` must hold the line.
    card(board, 9, "Upstream", texts={"Flyte": "https://console.example/runs/old"})
    _board.Reconciler(profile, "token", lambda r: (False, url, snapshot(**PASSED))).tick()
    assert board.items[ref(9)]["status"] == "Upstream"


def test_the_supervisors_done_never_overwrites_upstream(truth_world):
    profile, board, _ = truth_world
    card(board, 9, "Upstream")
    supervisor = _board.Supervisor(profile, "token", ref(9), lambda *a: None)
    supervisor.move("Done")
    assert board.items[ref(9)]["status"] == "Upstream"
