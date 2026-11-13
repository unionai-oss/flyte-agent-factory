"""GitHub evidence moves cards even when no project run was launched."""

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import flyte
import httpx
import pytest

from flyte_agent_factory import _board_events
from tests.test_github import gh as gh
from tests.test_reconcile import Board, board_profile
from tests.test_webhook import intake as intake

REPO = "example/scratch"


class App:
    def __init__(self):
        self.issues = {}
        self.posted = []
        self.closed = []
        self.commits = []
        self.pulls = []
        self.branches = []
        self.statuses = []
        self.head_reads = []
        self.reachable = True
        self.default = "main"
        self.events = []
        self.commit_time = "2026-01-01T00:00:00Z"

    def default_branch(self, repo):
        return self.default

    def commit_is_on_default_branch(self, repo, sha):
        return self.reachable

    def issue(self, repo, number):
        return {"state": self.issues.get(number, "open")}

    def repo_issues(self, repo, *, max_pages=None):
        return [{"number": n, "state": self.issues.get(n, "open")} for n in self.numbers()]

    def numbers(self):
        return {27, 49} | self.issues.keys()

    def issue_events(self, repo, number):
        return self.events

    def commit(self, repo, sha):
        return {"commit": {"committer": {"date": self.commit_time}}}

    def comments(self, repo, number):
        return [{"body": body} for n, body in self.posted if n == number]

    def issue_comments_since(self, repo, number, since=""):
        return self.comments(repo, number)

    def comment(self, repo, number, body):
        self.posted.append((number, body))

    def close_issue(self, repo, number):
        self.issues[number] = "closed"
        self.closed.append(number)

    def main_commits(self, repo):
        return self.commits

    def repo_pulls(self, repo):
        return self.pulls

    def branch_heads(self, repo, names):
        self.head_reads.append(list(names))
        return [branch for branch in self.branches if branch["name"] in names]

    def set_status(self, *args):
        self.statuses.append(args)

    def commit_statuses(self, repo, sha):
        latest = {}
        for _, status_sha, context, state, description, url in self.statuses:
            if status_sha == sha:
                latest[context] = {"context": context, "state": state, "description": description}
                latest[context]["target_url"] = url
        return list(latest.values())


@pytest.fixture
def world(monkeypatch):
    profile = dataclasses.replace(board_profile(app=True), repo=f"https://github.com/{REPO}.git")
    board = Board({f"{REPO}#{n}": {"status": "Backlog"} for n in (27, 49)}).install(monkeypatch)
    app = App()
    board.issue_state = lambda ref: app.issues.get(int(ref.split("#")[1]), "open").upper()
    return profile, board, app


def event(kind="push", **kwargs):
    return {"repository": {"full_name": REPO}, "ref": "refs/heads/main", "after": "abc", **kwargs}


@pytest.mark.parametrize("keyword", ["Closes", "FIXES", "resolved", "board", "Board"])
def test_reference_keywords_and_foreign_repositories(keyword):
    assert _board_events.references(f"{keyword} #49", REPO) == {49: True}
    assert _board_events.references(f"{keyword} other/repo#49", REPO) == {}
    assert _board_events.references(f"{keyword} https://github.com/other/repo/issues/49", REPO) == {}
    assert _board_events.references(f"{keyword} {REPO}#49", REPO) == {49: True}


def test_partial_references():
    assert _board_events.references("board #27 chunk 2", REPO) == {27: False}
    assert _board_events.references("#27 chunk 2", REPO) == {27: False}


def test_main_merge_and_duplicate_delivery(world):
    profile, board, app = world
    payload = event(commits=[{"id": "abc", "message": "Merge topic: board #49 done"}])
    for _ in range(2):
        _board_events.process(profile, "token", app, "push", payload)
    assert board.items[f"{REPO}#49"]["status"] == "Done"
    assert app.closed == [49]
    assert len(app.posted) == 1 and "abc" in app.posted[0][1]
    assert len(board.writes) == 1


def test_partial_push_and_done_never_moves_back(world):
    profile, board, app = world
    payload = event(commits=[{"id": "abc", "message": "board #27 chunk 2"}])
    _board_events.process(profile, "token", app, "push", payload)
    assert board.items[f"{REPO}#27"]["status"] == "In flight"
    assert app.closed == []
    board.items[f"{REPO}#27"]["status"] = "Done"
    _board_events.process(profile, "token", app, "push", payload)
    assert board.items[f"{REPO}#27"]["status"] == "Done"


def test_pr_review_merge_and_foreign_event(world):
    profile, board, app = world
    pull = {"body": "Closes #49", "title": "work", "head": {"ref": "work"}, "base": {"ref": "main"}}
    _board_events.process(profile, "token", app, "pull_request", event(action="opened", pull_request=pull))
    assert board.items[f"{REPO}#49"]["status"] == "In review"
    _board_events.process(
        profile, "token", app, "pull_request", event(action="closed", pull_request={**pull, "merged": True})
    )
    assert board.items[f"{REPO}#49"]["status"] == "Done"
    assert app.closed == []
    _board_events.process(
        profile,
        "token",
        app,
        "push",
        {**event(commits=[{"id": "abc", "message": "board #27"}]), "repository": {"full_name": "other/repo"}},
    )
    assert board.items[f"{REPO}#27"]["status"] == "Backlog"


def test_branch_first_push(world):
    profile, board, app = world
    _board_events.process(
        profile,
        "token",
        app,
        "push",
        event(
            ref="refs/heads/claude/work",
            created=True,
            commits=[{"id": "abc", "message": "board #27"}],
        ),
    )
    assert board.items[f"{REPO}#27"]["status"] == "In flight"
    assert app.closed == []


def test_drift_heals_and_flags(world):
    profile, board, app = world
    board.items[f"{REPO}#27"]["status"] = "Done"
    app.issues[49] = "closed"
    app.commits = [{"sha": "abc", "commit": {"message": "unrelated"}}]
    rows = _board_events.drift(profile, "token", app, lambda ref: None)
    assert board.items[f"{REPO}#49"]["status"] == "Done"
    assert any("Done but issue open" in row for row in rows)
    assert any("closed but card" in row for row in rows)
    assert app.statuses[-1][3] == "success"


def test_drift_flags_direct_merge_without_closing_open_issue(world):
    profile, board, app = world
    app.commits = [{"sha": "abc", "commit": {"message": "Merge topic: board #49"}}]
    rows = _board_events.drift(profile, "token", app, lambda ref: None)
    assert board.items[f"{REPO}#49"]["status"] == "Backlog"
    assert app.closed == []
    assert app.issue(REPO, 49)["state"] == "open"
    assert any("merged on main" in row and "needs review" in row for row in rows)
    assert app.statuses[-1][4] == "2 cards checked; 0 healed; 1 need review"


def test_stale_flight_and_review_without_pr(world):
    profile, board, app = world
    board.items[f"{REPO}#27"]["status"] = "In flight"
    board.items[f"{REPO}#49"]["status"] = "In review"
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    app.commits = [{"sha": "abc", "commit": {"message": "nothing"}}]
    app.branches = [{"name": "topic", "commit": {"message": "board #27", "committer": {"date": old}}}]
    rows = _board_events.drift(profile, "token", app, lambda ref: None)
    assert any("no activity for 7 days" in row for row in rows)
    assert any("In review without open PR" in row for row in rows)


async def test_signed_webhook_submits_board_work_when_intake_and_deploy_are_off(intake, monkeypatch):
    from types import SimpleNamespace

    from flyte_agent_factory import _profile, _webhook
    from tests.test_webhook import deliver

    profile = dataclasses.replace(
        intake.profile,
        repo="https://github.com/acme/workspace.git",  # the board's issues live in the workspace
        board=board_profile().board,
        deploy=_profile.Deploy(auto=False),
        github=dataclasses.replace(intake.profile.github, maintainers=()),
    )
    monkeypatch.setattr(_webhook, "PROFILE", profile)
    submitted = []

    async def submit(task, **kwargs):
        submitted.append((task, kwargs))

    monkeypatch.setattr(
        flyte,
        "with_runcontext",
        lambda **kw: SimpleNamespace(
            run=SimpleNamespace(aio=submit),
        ),
    )
    response = await deliver(intake, repo="acme/workspace", commits=[{"id": "abc", "message": "board #49"}])
    assert response.status_code == 202
    assert submitted[0][0]["name"] == _webhook.ISSUES_TASK
    assert json.loads(submitted[0][1]["payload"])["commits"][0]["message"] == "board #49"


def test_push_comments_even_when_github_already_closed_issue(world):
    profile, board, app = world
    app.issues[49] = "closed"
    payload = event(commits=[{"id": "abc", "message": "Closes #49"}])
    _board_events.process(profile, "token", app, "push", payload)
    assert len(app.posted) == 1 and "abc" in app.posted[0][1]
    assert app.closed == []
    assert board.items[f"{REPO}#49"]["status"] == "Done"


@pytest.mark.parametrize("evidence", ["branch", "run", "pr"])
def test_active_work_is_not_flagged_as_stale(world, evidence):
    profile, board, app = world
    board.items[f"{REPO}#27"]["status"] = "In flight"
    board.items[f"{REPO}#27"]["texts"]["Branch"] = "claude/work"
    if evidence == "branch":
        app.branches = [
            {
                "name": "claude/work",
                "commit": {
                    "message": "follow-up without issue reference",
                    "committer": {"date": datetime.now(timezone.utc).isoformat()},
                },
            }
        ]
    elif evidence == "pr":
        app.pulls = [{"title": "work", "body": "Closes #27"}]

    def latest(ref):
        return evidence == "run", "url", None

    assert _board_events.drift(profile, "token", app, latest) == []


async def test_hourly_entry_point_flags_open_issue_without_project_runs(world, monkeypatch):
    from flyte_agent_factory import _board, _github, _pull_requests, _reconcile

    profile, board, app = world
    app.commits = [{"sha": "abc", "commit": {"message": "Merge branch: board #49"}}]
    monkeypatch.setattr(_reconcile, "PROFILE", profile)
    monkeypatch.setattr(_reconcile, "project_runs", lambda: [])
    monkeypatch.setattr(_board, "take_token", lambda *args: "token")
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda *args: "key")
    monkeypatch.setattr(_github, "GithubApp", lambda *args: app)
    result = await _reconcile.reconcile()
    assert board.items[f"{REPO}#49"]["status"] == "Backlog"
    assert app.closed == []
    assert app.issue(REPO, 49)["state"] == "open"
    assert "1 board drift row(s)" in result
    assert app.statuses[-1][4] == "2 cards checked; 0 healed; 1 need review"


@pytest.mark.parametrize(
    "message",
    [
        'Revert "board #49"',
        'Revert "board #49"\n\nCloses #49',
        "Document rollback\n> Closes #49",
        'Merge pull request #123\n\nRevert "board #49"',
        "> Closes #49",
    ],
)
def test_commit_completion_ignores_reverts_and_body(world, message):
    profile, board, app = world
    _board_events.process(profile, "token", app, "push", event(commits=[{"id": "abc", "message": message}]))
    assert board.items[f"{REPO}#49"]["status"] == "Backlog"
    assert app.closed == []


@pytest.mark.parametrize("forced, reachable", [(True, True), (False, False)])
def test_stale_or_forced_push_cannot_complete(world, forced, reachable):
    profile, board, app = world
    app.reachable = reachable
    _board_events.process(
        profile,
        "token",
        app,
        "push",
        event(forced=forced, after="abc", commits=[{"id": "abc", "message": "board #49"}]),
    )
    assert board.items[f"{REPO}#49"]["status"] != "Done"
    assert app.closed == []


def test_drift_respects_repeated_human_reopens(world):
    profile, board, app = world
    app.commits = [{"sha": "abc", "commit": {"message": "board #49"}}]
    for _ in range(3):
        app.issues[49] = "open"
        rows = _board_events.drift(profile, "token", app, lambda ref: None)
        assert any("needs review" in row for row in rows)
        assert app.issue(REPO, 49)["state"] == "open"
    assert app.closed == []
    assert board.items[f"{REPO}#49"]["status"] == "Backlog"


@pytest.mark.parametrize("current", ["Done", "In review"])
def test_move_rereads_status_before_writing(world, monkeypatch, current):
    from flyte_agent_factory import _board

    profile, board, app = world
    original = _board.BoardClient

    def client(token):
        instance = original(token)
        original_items = instance.items

        def items():
            snapshot = original_items()
            board.items[f"{REPO}#49"]["status"] = current
            return snapshot

        monkeypatch.setattr(instance, "items", items)
        return instance

    monkeypatch.setattr(_board, "BoardClient", client)
    _board_events.process(profile, "token", app, "create", event(ref_type="branch", ref="work-49-#49"))
    assert board.items[f"{REPO}#49"]["status"] == current
    assert not [write for write in board.writes if write[1] == "Status"]


def test_delayed_push_does_not_regress_review(world):
    profile, board, app = world
    board.items[f"{REPO}#49"]["status"] = "In review"
    _board_events.process(
        profile,
        "token",
        app,
        "push",
        event(ref="refs/heads/work", commits=[{"id": "abc", "message": "board #49"}]),
    )
    assert board.items[f"{REPO}#49"]["status"] == "In review"


def test_merge_into_topic_does_not_complete(world):
    profile, board, app = world
    pull = {"title": "board #49", "merged": True, "base": {"ref": "topic"}}
    _board_events.process(profile, "token", app, "pull_request", event(action="closed", pull_request=pull))
    assert board.items[f"{REPO}#49"]["status"] != "Done"
    assert app.closed == []


# The factory lifecycle; Todo was removed from the board (inbox 036).
@pytest.mark.parametrize("status", ["Backlog", "Ready", "In flight", "In review", "Done"])
def test_unmerged_pr_only_rolls_review_back(world, status):
    profile, board, app = world
    board.items[f"{REPO}#49"]["status"] = status
    pull = {"title": "board #49", "merged": False}
    _board_events.process(profile, "token", app, "pull_request", event(action="closed", pull_request=pull))
    expected = "Done" if status == "Done" else "In flight"
    assert board.items[f"{REPO}#49"]["status"] == expected
    assert app.closed == []


@pytest.mark.parametrize(
    "head, forced, expected", [("abc", False, "Done"), ("stale", False, "Backlog"), ("abc", True, "Backlog")]
)
def test_main_completion_without_app_moves_and_flags(world, monkeypatch, caplog, head, forced, expected):

    import httpx

    profile, board, _ = world
    original = board.handle

    def handle(request):
        if "DefaultBranch" in json.loads(request.content)["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {"repository": {"defaultBranchRef": {"name": "main", "target": {"oid": "abc"}}}}
                },
            )
        return original(request)

    monkeypatch.setattr(board, "handle", handle)
    _board_events.process(
        profile,
        "token",
        None,
        "push",
        event(after=head, forced=forced, commits=[{"id": "abc", "message": "board #49"}]),
    )
    assert board.items[f"{REPO}#49"]["status"] == expected
    assert ("issue could not be closed" in caplog.text) == (expected == "Done")


@pytest.mark.parametrize("message", ['Revert "board #49"', "unrelated\nCloses #49", "> board #49"])
def test_drift_ignores_reverted_or_quoted_commit_references(world, message):
    profile, board, app = world
    app.commits = [{"sha": "abc", "commit": {"message": message}}]
    assert _board_events.drift(profile, "token", app, lambda ref: None) == []
    assert app.closed == []


def test_completion_uses_repository_default_branch(world):
    profile, board, app = world
    app.default = "trunk"
    _board_events.process(
        profile,
        "token",
        app,
        "push",
        event(ref="refs/heads/trunk", commits=[{"id": "abc", "message": "board #49"}]),
    )
    assert app.closed == [49]
    assert board.items[f"{REPO}#49"]["status"] == "Done"


def test_drift_flags_a_status_column_the_factory_never_sets(world):
    profile, board, app = world
    status = next(f for f in board.fields if f["name"] == "Status")
    status["options"] = [*status["options"], {"id": "opt-todo", "name": "Todo"}]
    rows = _board_events.drift(profile, "token", app, lambda ref: None)
    assert any("outside the factory lifecycle: Todo" in row for row in rows)


def test_one_status_list_drives_ranks_and_order():
    from flyte_agent_factory import _board

    assert _board.STATUSES == _board.SELECTS["Status"] == _board._STATUS_ORDER
    assert _board.STATUSES == ("Backlog", "Ready", "In flight", "In review", "Done")


# --- QA round 2 (N7-N10) ----------------------------------------------------------------------- #


def test_drift_status_is_written_only_when_the_flagged_rows_change(world):
    profile, board, app = world
    app.commits = [{"sha": "abc", "commit": {"message": "Merge topic: board #49"}}]
    for _ in range(3):
        _board_events.drift(profile, "token", app, lambda ref: None)
    assert len(app.statuses) == 1
    app.issues[49] = "closed"  # the flag clears: one more status says so
    _board_events.drift(profile, "token", app, lambda ref: None)
    _board_events.drift(profile, "token", app, lambda ref: None)
    assert len(app.statuses) == 2


def test_drift_without_flagged_rows_writes_no_status(world):
    profile, board, app = world
    board.items[f"{REPO}#27"]["status"] = "Done"
    app.issues[27] = "closed"
    app.commits = [{"sha": "abc", "commit": {"message": "unrelated"}}]
    assert _board_events.drift(profile, "token", app, lambda ref: None) == []
    assert app.statuses == []


@pytest.mark.parametrize(
    "context, wakes",
    [(_board_events.STATUS_CONTEXT, False), ("ci/tests", True)],
)
def test_the_factorys_drift_status_never_wakes_reconcile(world, monkeypatch, context, wakes):
    from flyte_agent_factory import _webhook

    repos = {"factory": f"https://github.com/{REPO}.git"}
    monkeypatch.setattr(_webhook, "PROFILE", dataclasses.replace(world[0], repos=repos))
    payload = {"repository": {"full_name": REPO}, "state": "success", "context": context}
    assert _webhook._outcome_event("status", payload) is wakes


def reopened_after_completion(world):
    profile, board, app = world
    payload = event(commits=[{"id": "abc", "message": "Closes #49"}])
    _board_events.process(profile, "token", app, "push", payload)
    assert app.closed == [49]
    app.issues[49] = "open"  # a human reopens it
    return profile, board, app, payload


def test_delayed_push_does_not_reclose_a_reopened_issue(world):
    profile, board, app, payload = reopened_after_completion(world)
    _board_events.process(profile, "token", app, "push", payload)
    assert app.closed == [49]
    assert app.issue(REPO, 49)["state"] == "open"
    assert len(app.posted) == 1


def test_a_failed_close_is_retried_by_the_same_push(world):
    profile, board, app = world
    payload = event(commits=[{"id": "abc", "message": "Closes #49"}])
    close = app.close_issue
    app.close_issue = lambda *args: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        _board_events.process(profile, "token", app, "push", payload)
    app.close_issue = close
    _board_events.process(profile, "token", app, "push", payload)
    assert app.closed == [49]
    assert board.items[f"{REPO}#49"]["status"] == "Done"


def test_a_new_commit_may_complete_a_reopened_issue_again(world):
    profile, board, app, payload = reopened_after_completion(world)
    _board_events.process(
        profile, "token", app, "push", event(after="def", commits=[{"id": "def", "message": "Closes #49"}])
    )
    assert app.closed == [49, 49]


def test_delayed_closed_delivery_does_not_complete_an_open_issue(world):
    profile, board, app = world
    app.issues[49] = "open"
    _board_events.process(
        profile, "token", app, "issues", event(action="closed", issue={"number": 49}, ref=None)
    )
    assert board.items[f"{REPO}#49"]["status"] == "Backlog"
    app.issues[49] = "closed"
    _board_events.process(
        profile, "token", app, "issues", event(action="closed", issue={"number": 49}, ref=None)
    )
    assert board.items[f"{REPO}#49"]["status"] == "Done"


def test_concurrent_deliveries_never_move_done_backwards(world, monkeypatch):
    import threading
    import time

    profile, board, app = world
    handle = board.handle

    def slow(request):
        # Both deliveries reread Backlog; the completion lands before the branch delivery's late write.
        body = json.loads(request.content)
        if "SetField" in body["query"] and body["variables"]["input"]["fieldId"] == "Status":
            time.sleep(0.1 if body["variables"]["input"]["value"]["singleSelectOptionId"] == "Done" else 0.4)
        return handle(request)

    monkeypatch.setattr(board, "handle", slow)
    complete = event(commits=[{"id": "abc", "message": "Closes #49"}])
    branch = event(ref_type="branch", ref="work-49-#49")
    threads = [
        threading.Thread(target=_board_events.process, args=(profile, "token", app, "push", complete)),
        threading.Thread(target=_board_events.process, args=(profile, "token", app, "create", branch)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    statuses = [value for ref, field, value in board.writes if ref == f"{REPO}#49" and field == "Status"]
    assert statuses in (["Done"], ["In flight", "Done"])


@pytest.mark.parametrize("flyte", ["", "https://console.example/runs/r1"])
def test_supervisor_never_moves_an_open_issues_done_card_back(world, flyte):
    from flyte_agent_factory import _board

    profile, board, app = world
    board.items[f"{REPO}#49"]["status"] = "Done"
    if flyte:
        board.items[f"{REPO}#49"]["texts"]["Flyte"] = flyte
    worker = _board.supervisor(profile, "token", f"{REPO}#49", lambda *row: None)
    assert worker is not None
    worker.move("In review", Flyte="https://console.example/runs/r1", Branch="work")
    assert board.items[f"{REPO}#49"]["status"] == "Done"
    assert not worker.pending


def test_supervisor_of_a_new_run_still_restarts_an_older_runs_done_card(world):
    from flyte_agent_factory import _board

    profile, board, app = world
    board.items[f"{REPO}#49"].update(status="Done", texts={"Flyte": "https://console.example/runs/r0"})
    worker = _board.supervisor(profile, "token", f"{REPO}#49", lambda *row: None)
    assert worker is not None
    worker.move("In flight", Flyte="https://console.example/runs/r1")
    assert board.items[f"{REPO}#49"]["status"] == "In flight"


def test_drift_reads_heads_only_for_named_branches_at_most_100(world):
    profile, board, app = world
    board.items.clear()
    for n in range(1, 151):
        board.items[f"{REPO}#{n}"] = {"status": "In flight", "texts": {"Branch": f"work-{n}"}}
    board.items[f"{REPO}#999"] = {"status": "In flight", "texts": {}}
    app.commits = [{"sha": "abc", "commit": {"message": "unrelated"}}]
    _board_events.drift(profile, "token", app, lambda ref: None)
    (names,) = app.head_reads
    assert len(names) == 100 and set(names) <= {f"work-{n}" for n in range(1, 151)}


def test_drift_skips_the_branch_read_when_no_card_names_one(world):
    profile, board, app = world
    app.commits = [{"sha": "abc", "commit": {"message": "unrelated"}}]
    _board_events.drift(profile, "token", app, lambda ref: None)
    assert app.head_reads == []


def test_supervisor_reopens_done_only_when_it_says_so(world):
    from flyte_agent_factory import _board

    profile, board, app = world
    run = "https://console.example/runs/r1"
    board.items[f"{REPO}#49"].update(status="Done", texts={"Flyte": run})
    worker = _board.supervisor(profile, "token", f"{REPO}#49", lambda *row: None)
    assert worker is not None
    worker.move("In review", Flyte=run)
    assert board.items[f"{REPO}#49"]["status"] == "Done"
    worker.move("In review", Flyte=run, reopen_done=True)
    assert board.items[f"{REPO}#49"]["status"] == "In review"


def test_cross_process_late_review_is_healed_next_pass(world, monkeypatch):
    from flyte_agent_factory import _board

    profile, board, app = world
    original = _board.BoardClient
    raced = False

    def client(token):
        nonlocal raced
        result = original(token)
        write = result.set_status

        def late_review(item, status):
            nonlocal raced
            if status == "In review" and not raced:
                raced = True
                _board_events.process(
                    profile, token, app, "push", event(commits=[{"id": "abc", "message": "Closes #49"}])
                )
            write(item, status)

        monkeypatch.setattr(result, "set_status", late_review)
        return result

    # Separate processes have separate locks; removing serialization here simulates their overlap.
    from contextlib import nullcontext

    monkeypatch.setattr(_board, "WRITE_LOCK", nullcontext())
    monkeypatch.setattr(_board, "BoardClient", client)
    _board_events.process(
        profile, "token", app, "pull_request", event(action="opened", pull_request={"title": "Closes #49"})
    )
    assert app.issues[49] == "closed"
    assert board.items[f"{REPO}#49"]["status"] == "In review"
    _board_events.drift(profile, "token", app, lambda ref: None)
    assert board.items[f"{REPO}#49"]["status"] == "Done"


def test_drift_thousand_closed_cards_uses_bulk_states_and_single_nodes(world, monkeypatch, gh):
    profile, board, app = world
    board.items.clear()
    for n in range(1, 1001):
        board.items[f"{REPO}#{n}"] = {"status": "Done" if n < 900 else "Backlog", "texts": {}}
        app.issues[n] = "closed"
    calls = []
    handle = board.handle

    def record(request):
        calls.append(json.loads(request.content)["query"])
        return handle(request)

    monkeypatch.setattr(board, "handle", record)
    rest = []

    def handle_rest(request):
        rest.append(request)
        path = request.url.path
        if path == f"/repos/{REPO}/issues":
            assert request.url.params["state"] == "all"
            assert request.url.params["per_page"] == "100"
            page = int(request.url.params["page"])
            assert page <= 10
            return httpx.Response(
                200,
                json=[{"number": n, "state": "closed"} for n in range((page - 1) * 100 + 1, page * 100 + 1)],
            )
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main"})
        if path in (f"/repos/{REPO}/commits", f"/repos/{REPO}/pulls"):
            return httpx.Response(200, json=[])
        pytest.fail(f"unexpected REST call: {path}")

    monkeypatch.setattr(gh.app, "installation_token", lambda repo: "token")
    gh.app._http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handle_rest))
    _board_events.drift(profile, "token", gh.app, lambda ref: None)
    assert len(rest) == 13
    assert sum("BoardItems" in query for query in calls) == 1
    assert sum("BoardItem(" in query for query in calls) == 101
    assert all(item["status"] == "Done" for item in board.items.values())


def test_drift_unknown_status_heals(world):
    profile, board, app = world
    board.items[f"{REPO}#49"]["status"] = "Custom"
    app.issues[49] = "closed"
    rows = _board_events.drift(profile, "token", app, lambda ref: None)
    assert board.items[f"{REPO}#49"]["status"] == "Done"
    assert any("healed" in row for row in rows)


@pytest.mark.parametrize("changed", ["reopened", "already_done", "removed"])
def test_drift_checks_fresh_issue_inside_lock_and_logs_only_moves(world, monkeypatch, caplog, changed):
    from flyte_agent_factory import _board

    profile, board, app = world
    app.issues[49] = "closed"

    class Lock:
        def __enter__(self):
            if changed == "reopened":
                app.issues[49] = "open"
            elif changed == "already_done":
                board.items[f"{REPO}#49"]["status"] = "Done"
            else:
                del board.items[f"{REPO}#49"]

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(_board, "WRITE_LOCK", Lock())
    rows = _board_events.drift(profile, "token", app, lambda ref: None)
    assert not board.writes
    assert not any("healed" in row for row in rows)
    assert "closed but card not Done; healed" not in caplog.text


def test_first_late_delivery_respects_reopen_after_commit_without_marker(world):
    profile, board, app = world
    app.events = [{"event": "reopened", "created_at": "2026-01-02T00:00:00Z"}]
    _board_events.process(
        profile, "token", app, "push", event(commits=[{"id": "abc", "message": "Closes #49"}])
    )
    assert app.closed == []
    assert app.posted == []
    assert board.items[f"{REPO}#49"]["status"] == "Backlog"


@pytest.mark.parametrize("reopened_at", ["2025-12-31T23:59:59Z", "2026-01-01T00:00:00Z"])
def test_reopen_before_or_at_commit_allows_completion(world, reopened_at):
    profile, board, app = world
    app.events = [{"event": "reopened", "created_at": reopened_at}]
    _board_events.process(
        profile, "token", app, "push", event(commits=[{"id": "abc", "message": "Closes #49"}])
    )
    assert app.closed == [49]
    assert board.items[f"{REPO}#49"]["status"] == "Done"


def test_drift_logs_branch_read_cap(world, caplog):
    profile, board, app = world
    board.items.clear()
    for n in range(1, 102):
        board.items[f"{REPO}#{n}"] = {"status": "In flight", "texts": {"Branch": f"topic-{n}"}}
        app.issues[n] = "open"
    _board_events.drift(profile, "token", app, lambda ref: None)
    assert "branch reads truncated at 100" in caplog.text


def test_drift_heals_closed_issue_missing_from_capped_rest_listing(world, monkeypatch):
    profile, board, app = world
    app.issues[49] = "closed"
    monkeypatch.setattr(app, "repo_issues", lambda *args, **kwargs: [])
    _board_events.drift(profile, "token", app, lambda ref: None)
    assert board.items[f"{REPO}#49"]["status"] == "Done"


def test_an_unchanged_flagged_set_is_not_republished_when_main_advances(world):
    # QA #55 round 6: the digest was compared only with the current head's statuses.
    profile, board, app = world
    app.commits = [{"sha": "aaa", "commit": {"message": "Merge topic: board #49"}}]
    _board_events.drift(profile, "token", app, lambda ref: None)
    assert len(app.statuses) == 1
    app.commits = [{"sha": "bbb", "commit": {"message": "unrelated"}}, *app.commits]
    _board_events.drift(profile, "token", app, lambda ref: None)
    assert len(app.statuses) == 1
    app.issues[49] = "closed"  # the flagged set changes: one new status, on the new head
    _board_events.drift(profile, "token", app, lambda ref: None)
    assert len(app.statuses) == 2 and app.statuses[-1][1] == "bbb"


def test_board_issues_live_in_the_workspace_whatever_the_factory_repo(world):
    # The engine is just another target: `[repo] factory` does not move the board's home off the workspace.
    profile, _, app = world
    profile = dataclasses.replace(
        profile, repos={**profile.repos, "factory": "https://github.com/acme/engine.git"}
    )
    assert _board_events.repository(profile) == REPO
    assert _board_events.eligible(profile, "push", event())
    assert not _board_events.eligible(
        profile, "push", {**event(), "repository": {"full_name": "acme/engine"}}
    )
