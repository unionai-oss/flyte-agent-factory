"""Read-only admission from board, run, PR and capacity facts."""

import re
from dataclasses import replace

import pytest

from flyte_agent_factory import _planner as p
from flyte_agent_factory._meter import Meter


def item(number, files=None, **kwargs):
    kwargs.setdefault("status", "Ready")
    body = "A task" if files is None else "## Files\n" + "\n".join(f"- `{path}`" for path in files)
    return p.BoardItem(f"I{number}", number, "example/repo", position=number, body=body, **kwargs)


def facts(*items, slots=3, **kwargs):
    return p.Facts(
        items=items,
        capacity=tuple(p.Capacity("claude", f"login-{i}") for i in range(slots)),
        now=1000,
        **kwargs,
    )


def number(entry: p.BoardItem | p.ManualRequest) -> int:
    assert isinstance(entry, p.BoardItem)
    return entry.number


def launched(result):
    return [number(launch.item) for launch in result.launches]


def test_priority_then_board_position_and_capacity():
    items = [
        item(4, ["d"], labels=("priority",)),
        item(3, ["c"]),
        item(1, ["a"]),
        item(2, ["b"], labels=("priority",)),
    ]
    result = p.plan(facts(*items))
    assert launched(result) == [2, 4, 1]
    assert number(result.waiting[0].item) == 3
    assert result.waiting[0].reason.code == "no_capacity"
    assert len({launch.login for launch in result.launches}) == 3


def test_blocked_first_does_not_block_disjoint_work():
    run = p.ActiveRun("busy", "example/repo", predicted_files=("src/a.py",))
    result = p.plan(facts(item(1, ["src/a.py"]), item(2, ["docs/readme.md"]), active_runs=(run,)))
    assert launched(result) == [2]
    assert result.waiting[0].reason == p.Reason("file_conflict", "busy")


@pytest.mark.parametrize(
    "left,right",
    [
        ("src/a.py", "src/a.py"),
        ("src", "src/a.py"),
        ("src/", "src/pkg/a.py"),
        ("src/*.py", "src/a.py"),
        ("src/**/a.py", "src/a.py"),
        ("src/a*.py", "src/b*.py"),
        ("src/pkg", "src/*/a.py"),
    ],
)
def test_overlapping_scopes(left, right):
    result = p.plan(facts(item(1, [left]), item(2, [right])))
    assert launched(result) == [1]
    assert result.waiting[0].reason.code == "file_conflict"


def test_distinct_glob_and_literal_do_not_conflict():
    assert launched(p.plan(facts(item(1, ["src/*.py"]), item(2, ["src/a.txt"])))) == [1, 2]


def test_unknown_scope_is_whole_canonical_repo():
    result = p.plan(facts(item(1), item(2, ["docs/a"]), replace(item(3), repo="example/other")))
    assert launched(result) == [1, 3]
    run = p.ActiveRun("legacy", "git@github.com:Example/Repo.git")
    assert not p.plan(facts(item(1, ["anything"]), active_runs=(run,))).launches


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../a", "src/../a", "a/[", "a/[]", "a/{b,c}", "C:/a", "a\\b", "", "a/\x00"],
)
def test_invalid_files(path):
    result = p.plan(facts(item(1, [path])))
    assert not result.launches
    assert result.waiting[0].reason.code == "invalid_files"


def test_explicit_empty_files_is_invalid():
    result = p.plan(facts(replace(item(1), body="## Files\n\n## Acceptance\nDo it")))
    assert result.waiting[0].reason.code == "invalid_files"


def test_normalization_and_tracked_path_heuristic_are_reported():
    result = p.plan(
        facts(
            replace(item(1), body="Please edit `src/a.py`, then run tests."),
            tracked_paths={"example/repo": {"src/a.py", "a.py"}},
        )
    )
    assert result.launches[0].predicted_files == ("src/a.py",)
    assert "heuristic" in result.launches[0].reason
    assert p.plan(facts(item(1, ["./src//a.py"]))).launches[0].predicted_files == ("src/a.py",)


def test_pr_and_observed_run_paths_remain_occupied():
    run = p.ActiveRun("run", "example/repo", predicted_files=("a",), observed_files=("b",))
    pr = p.OpenPR("example/repo", ("c",), issue="I9", identity="pr-9")
    result = p.plan(facts(item(1, ["b"]), item(2, ["c"]), item(9, ["d"]), active_runs=(run,), open_prs=(pr,)))
    assert [wait.reason for wait in result.waiting] == [
        p.Reason("file_conflict", "run"),
        p.Reason("file_conflict", "pr-9"),
        p.Reason("pr_open", "pr-9"),
    ]


def test_issue_identity_prevents_duplicate_run_even_when_files_are_disjoint():
    run = p.ActiveRun("run", "example/repo", issue="I1", generation="2", predicted_files=("other",))
    result = p.plan(facts(item(1, ["a"]), active_runs=(run,)))
    assert result.waiting[0].reason == p.Reason("already_running", "run")
    terminal = replace(run, phase="SUCCEEDED")
    assert launched(p.plan(facts(item(1, ["a"]), active_runs=(terminal,)))) == [1]


def test_non_ready_closed_pr_and_draft_cards_do_not_launch():
    result = p.plan(
        facts(
            item(1, ["a"], status="Backlog"),
            item(2, ["b"], type="pr"),
            item(3, ["c"], type="draft"),
            item(4, ["d"], open=False),
        )
    )
    assert not result.launches
    assert [w.reason.code for w in result.waiting] == ["not_issue", "not_issue", "closed"]


def test_a_low_manual_request_waits_while_a_normal_one_waits():
    garden = p.ManualRequest("gardening", "example/repo", files=("g",), priority="low")
    feature = p.ManualRequest("feature", "example/repo", files=("f",))
    result = p.plan(facts(item(1, ["b"]), manual_requests=(garden, feature), slots=1))
    assert [launch.item for launch in result.launches] == [feature]
    waiting = {
        w.item.node_id if isinstance(w.item, p.BoardItem) else w.item.name: w.reason for w in result.waiting
    }
    assert waiting["gardening"] == p.Reason("low_priority_waits")


def test_a_low_manual_request_starts_when_nothing_else_waits():
    garden = p.ManualRequest("gardening", "example/repo", files=("g",), priority="low")
    result = p.plan(facts(manual_requests=(garden,), slots=1))
    assert [launch.item for launch in result.launches] == [garden]


def test_an_expedite_manual_request_goes_before_a_ready_board_item():
    urgent = p.ManualRequest("urgent", "example/repo", files=("u",), priority="expedite")
    result = p.plan(facts(item(1, ["b"]), manual_requests=(urgent,), slots=1))
    assert [launch.item for launch in result.launches] == [urgent]


def test_a_reserved_manual_request_goes_before_a_ready_board_item():
    garden = p.ManualRequest("gardening", "example/repo", files=("g",), priority="reserved")
    result = p.plan(facts(item(1, ["b"]), manual_requests=(garden,), slots=1))
    assert [launch.item for launch in result.launches] == [garden]


def test_an_expedite_label_takes_a_board_item_ahead_of_a_normal_manual_request():
    manual = p.ManualRequest("manual", "example/repo", files=("m",))
    result = p.plan(facts(item(1, ["b"], labels=("Expedite",)), manual_requests=(manual,), slots=1))
    assert launched(result) == [1]


def test_manual_requests_share_capacity_scope_and_issue_dedupe():
    manual = p.ManualRequest("manual", "example/repo", files=("a",), issue="I1")
    result = p.plan(facts(item(1, ["b"]), item(2, ["c"]), manual_requests=(manual,), slots=1))
    assert [launch.item for launch in result.launches] == [manual]
    assert [w.reason.code for w in result.waiting] == ["already_running", "no_capacity"]
    unknown = replace(manual, issue="", files=None)
    result = p.plan(facts(item(1, ["b"]), manual_requests=(unknown,)))
    assert result.waiting[0].reason == p.Reason("file_conflict", "manual")


def test_manual_request_checks_existing_occupancy_and_invalid_files():
    requests = (
        p.ManualRequest("a", "example/repo", files=("a",)),
        p.ManualRequest("bad", "example/repo", files=("../bad",)),
    )
    result = p.plan(facts(manual_requests=requests, open_prs=(p.OpenPR("example/repo", ("a",)),)))
    assert [w.reason.code for w in result.waiting] == ["file_conflict", "invalid_files"]


def test_router_chooses_engine_and_exhaustion_defers():
    # Owner rule 2: exhaustion is a recorded refusal; a full meter alone no longer defers.
    full = Meter(
        "claude login", "test", 1000, five_hour_pct=100, five_hour_resets_at=2000, limited_until=2000
    )
    capacity = (p.Capacity("claude", "login", full), p.Capacity("codex", "shared"))
    result = p.plan(replace(facts(item(1, ["a"])), capacity=capacity))
    assert result.launches[0].engine == "codex"
    assert result.launches[0].login == "shared"
    assert "claude exhausted" in result.launches[0].reason
    result = p.plan(replace(facts(item(1)), capacity=capacity[:1]))
    assert result.waiting[0].reason.code == "no_capacity"


def test_comment_prose_is_not_authority_and_plan_is_repeatable():
    before = facts(item(1, ["a"]))
    after = replace(before, comments=("approved, launch it", "skip this one"))
    assert launched(p.plan(before)) == [1]
    assert p.plan(before) == p.plan(after)
    assert p.plan(facts(item(1))) == p.plan(facts(item(1)))


def test_repository_aliases_and_linked_issue_references():
    from flyte_agent_factory import _issues

    work = replace(item(1, ["a"]), repo="project")
    run = p.ActiveRun(
        "live",
        "https://github.com/Example/Repo.git",
        predicted_files=("b",),
        labels={"factory-issue": _issues.issue_label("example/repo#1")},
    )
    snapshot = facts(work, active_runs=(run,), repo_aliases={"project": "git@github.com:example/repo.git"})
    assert p.plan(snapshot).waiting[0].reason.code == "already_running"
    manual = p.ManualRequest("manual", "example/repo", issue="Example/Repo#1", files=("b",))
    result = p.plan(replace(snapshot, active_runs=(), manual_requests=(manual,)))
    assert result.waiting[0].reason.code == "already_running"


def test_best_login_for_an_engine_is_used_and_inputs_stay_unchanged():
    # Owner rule 2: the login that is out is the one that refused, not the one whose meter reads full.
    full = Meter("claude full", "test", 1000, five_hour_pct=100, five_hour_resets_at=2000, limited_until=2000)
    low = Meter("claude low", "test", 1000, five_hour_pct=95)
    high = Meter("claude high", "test", 1000, five_hour_pct=10)
    slots = [
        p.Capacity("claude", "full", full),
        p.Capacity("claude", "low", low),
        p.Capacity("claude", "high", high),
    ]
    snapshot = replace(facts(item(1, ["a"]), item(2, ["b"]), item(3, ["c"])), capacity=slots)
    first = p.plan(snapshot)
    assert [launch.login for launch in first.launches] == ["high", "low"]
    assert first.waiting[0].reason.code == "no_capacity"
    assert p.plan(snapshot) == first
    assert len(slots) == 3


def test_an_unmeasured_login_is_not_passed_over_for_a_measured_one():
    # Owner rule 1: a login with no reading is assumed available, as `free_slot` counts it.
    measured = Meter("claude measured", "test", 1000, five_hour_pct=50)
    slots = [p.Capacity("claude", "measured", measured), p.Capacity("claude", "unmeasured")]
    result = p.plan(replace(facts(item(1, ["a"])), capacity=slots))
    assert [launch.login for launch in result.launches] == ["unmeasured"]


def test_an_unmeasured_claude_is_not_passed_over_for_a_measured_codex():
    measured = Meter("codex", "test", 1000, five_hour_pct=10)
    capacity = (p.Capacity("claude", "login"), p.Capacity("codex", "shared", measured))
    result = p.plan(replace(facts(item(1, ["a"])), capacity=capacity))
    assert result.launches[0].engine == "claude"
    assert "has headroom" not in result.launches[0].reason


@pytest.mark.parametrize("pattern", ["a/[z-a]", "src/**oops/a.py"])
def test_invalid_glob_contracts(pattern):
    assert p.plan(facts(item(1, [pattern]))).waiting[0].reason.code == "invalid_files"


def test_files_examples_in_a_code_fence_do_not_declare_scope():
    work = replace(item(1), body="```md\n## Files\n- a\n```\nEdit b.py.")
    result = p.plan(facts(work, tracked_paths={"example/repo": {"b.py"}}))
    assert result.launches[0].predicted_files == ("b.py",)
    assert "heuristic" in result.launches[0].reason


def test_missing_ready_status_does_not_admit_work():
    work = p.BoardItem("I1", 1, "example/repo", body="## Files\n- a")
    assert not p.plan(facts(work)).launches


def test_forty_items_against_fifty_thousand_tracked_paths_is_fast(monkeypatch):
    # "Fast" is that the work does not grow with the tracked paths (one tokenization per item and a set
    # intersection, not a regex per path). Counted rather than timed: a wall-clock bound flakes under load.
    regex_calls = []

    class CountingRe:
        def __getattr__(self, name):
            function = getattr(re, name)
            if not callable(function):
                return function

            def counted(*args, **kwargs):
                regex_calls.append(name)
                return function(*args, **kwargs)

            return counted

    monkeypatch.setattr(p, "re", CountingRe())
    tracked = {f"pkg{i // 100}/mod{i}.py" for i in range(50_000)}
    work = [replace(item(i), body=f"Change pkg1/mod{100 + i}.py, not x/y.py.") for i in range(1, 41)]
    snapshot = facts(*work, slots=50, tracked_paths={"example/repo": tracked})
    result = p.plan(snapshot)
    assert len(regex_calls) < 1000, (
        f"{len(regex_calls)} regex calls for 40 items: it scales with the tracked paths"
    )
    assert len(result.launches) == 40
    assert result.launches[0].predicted_files == ("pkg1/mod101.py",)


def test_unusable_engine_slot_is_skipped_and_recorded():
    capacity = (p.Capacity("gemini", "g"), p.Capacity("claude", "c"))
    result = p.plan(replace(facts(item(1, ["a"]), item(2, ["b"])), capacity=capacity))
    assert launched(result) == [1]
    assert result.waiting[0].reason.code == "no_capacity"
    assert result.unusable_slots == (p.Reason("unusable_slot", "g"),)
    only_bad = p.plan(replace(facts(item(1, ["a"])), capacity=capacity[:1]))
    assert only_bad.waiting[0].reason.code == "no_capacity"


def test_an_invalid_item_waits_and_the_pass_continues():
    bad = replace(item(1, ["a"]), repo="")
    odd = replace(item(2, ["b"]), repo="https://example.com/not-github")
    result = p.plan(facts(bad, odd, item(3, ["c"])))
    assert launched(result) == [3]
    assert [w.reason.code for w in result.waiting] == ["invalid_item", "invalid_item"]
    manual = p.ManualRequest("m", "", issue="not an issue", files=("z",))
    result = p.plan(facts(item(3, ["c"]), manual_requests=(manual,)))
    assert launched(result) == [3]
    assert result.waiting[0].reason.code == "invalid_item"


def test_priority_label_is_case_insensitive():
    result = p.plan(facts(item(1, ["a"]), item(2, ["b"], labels=("Priority",)), slots=1))
    assert launched(result) == [2]


def test_tracked_paths_match_literally_not_as_globs():
    tracked = {"example/repo": {"docs/[x].md", "docs/x.md"}}
    result = p.plan(
        facts(
            replace(item(1), body="Edit docs/[x].md"),
            item(2, ["docs/x.md"]),
            replace(item(3), body="Edit docs/x.md"),
            tracked_paths=tracked,
        )
    )
    assert launched(result) == [1, 2]
    assert result.launches[0].predicted_files == ("docs/[x].md",)
    assert result.waiting[0].reason.code == "file_conflict"
    run = p.ActiveRun("busy", "example/repo", predicted_files=("other",), observed_files=("docs/[x].md",))
    assert launched(p.plan(facts(item(1, ["docs/x.md"]), active_runs=(run,)))) == [1]
    pr = p.OpenPR("example/repo", ("docs/[x].md",), identity="pr")
    assert launched(p.plan(facts(item(1, ["docs/x.md"]), open_prs=(pr,)))) == [1]
    assert not p.plan(facts(item(1, ["docs/*.md"]), open_prs=(pr,))).launches


def test_file_conflict_names_a_heuristic_or_unknown_scope():
    run = p.ActiveRun("busy", "example/repo", predicted_files=("src/a.py",))
    guessed = replace(item(1), body="Edit src/a.py")
    snapshot = facts(guessed, item(2), item(3, ["src/a.py"]), active_runs=(run,))
    snapshot = replace(snapshot, tracked_paths={"example/repo": {"src/a.py"}})
    waits = {number(w.item): w.reason for w in p.plan(snapshot).waiting}
    assert waits[1] == p.Reason("file_conflict", "busy", "scope=heuristic")
    assert waits[3] == p.Reason("file_conflict", "busy")
    whole = p.ActiveRun("legacy", "example/repo")
    waits = {number(w.item): w.reason for w in p.plan(facts(item(2), active_runs=(whole,))).waiting}
    assert waits[2] == p.Reason("file_conflict", "legacy", "scope=unknown")


def test_linked_manual_request_uses_the_issue_scope_before_the_whole_repo():
    manual = p.ManualRequest("manual", "example/repo", issue="I1")
    result = p.plan(facts(item(1, ["a"]), item(2, ["b"]), manual_requests=(manual,)))
    assert result.launches[0].item == manual
    assert result.launches[0].predicted_files == ("a",)
    assert [number(launch.item) for launch in result.launches[1:]] == [2]
    guessed = replace(item(1), body="Edit src/a.py")
    snapshot = facts(guessed, tracked_paths={"example/repo": {"src/a.py"}}, manual_requests=(manual,))
    assert p.plan(snapshot).launches[0].predicted_files == ("src/a.py",)
    unlinked = p.ManualRequest("manual", "example/repo")
    assert p.plan(facts(manual_requests=(unlinked,))).launches[0].predicted_files == ()


def test_a_bare_issue_number_on_a_run_resolves_against_the_runs_repository():
    run = p.ActiveRun("live", "example/repo", issue="#1", predicted_files=("z",))
    assert p.plan(facts(item(1, ["a"]), active_runs=(run,))).waiting[0].reason == p.Reason(
        "already_running", "live"
    )
    other = replace(item(1, ["a"]), repo="example/other")
    assert launched(p.plan(facts(other, active_runs=(run,)))) == [1]


# --- facts_from: recorded fakes in the shape of BoardClient.items(), remote.Run and PR listings ---


def board_row(number, **kwargs):
    row = {
        "item_id": f"item-{number}",
        "node_id": f"I{number}",
        "repo": "Example/Repo",
        "number": number,
        "type": "issue",
        "open": True,
        "body": "A task",
        "position": number,
        "linked_prs": [],
        "linked_run": "",
        "title": f"issue {number}",
        "status": "Ready",
        "texts": {},
        "labels": [],
    }
    return {**row, **kwargs}


def fake_run(name, phase="RUNNING", **labels):
    from types import SimpleNamespace

    return SimpleNamespace(name=name, phase=phase, pb2=SimpleNamespace(labels=labels))


def test_facts_from_converts_board_items():
    pr = {"id": "PR1", "number": 7, "url": "https://github.com/example/repo/pull/7", "state": "OPEN"}
    row = board_row(1, labels=["priority"], linked_prs=[pr], linked_run="https://console/runs/x", body="b")
    found = p.facts_from([row, board_row(2, type="draft", repo="")], [], [], [], [], now=5)
    assert found.now == 5
    assert found.items[0] == p.BoardItem(
        "I1",
        1,
        "Example/Repo",
        type="issue",
        open=True,
        status="Ready",
        position=1,
        labels=("priority",),
        body="b",
        linked_run="https://console/runs/x",
        linked_prs=("https://github.com/example/repo/pull/7",),
    )
    assert found.items[1].type == "draft"


def test_facts_from_identifies_runs_by_the_issue_label_and_the_boards_repository():
    from flyte_agent_factory import _issues

    label = _issues.issue_label("example/repo#1")
    live = fake_run("live", "RunPhase.RUNNING", **{"factory-issue": label, "factory-gen": "3"})
    manual = fake_run("hand", "QUEUED")
    ended = fake_run("done", "SUCCEEDED", **{"factory-issue": label})
    found = p.facts_from(
        [board_row(1), board_row(2)], [live, manual, ended], [], [], [], default_repo="example/default"
    )
    assert [(r.name, r.repo, r.issue, r.generation, r.phase) for r in found.active_runs] == [
        ("live", "Example/Repo", label, "3", "RUNNING"),
        ("hand", "example/default", "", "", "QUEUED"),
        ("done", "Example/Repo", label, "", "SUCCEEDED"),
    ]
    result = p.plan(replace(found, capacity=(p.Capacity("claude", "a"),) * 3))
    assert [w.reason for w in result.waiting] == [
        p.Reason("already_running", "live"),
        p.Reason("file_conflict", "live", "scope=unknown"),
    ]


def test_facts_from_links_open_pull_requests_to_their_issue_and_files():
    url = "https://github.com/example/repo/pull/7"
    row = board_row(1, linked_prs=[{"id": "PR1", "number": 7, "url": url, "state": "OPEN"}])
    listing = [
        {"repo": "example/repo", "number": 7, "url": url, "changed_files": ["src/a.py"]},
        {"repo": "example/repo", "number": 8, "changed_files": []},
    ]
    found = p.facts_from([row], [], listing, [], [])
    assert found.open_prs == (
        p.OpenPR("example/repo", ("src/a.py",), issue="I1", identity=url),
        p.OpenPR("example/repo", (), issue="", identity="example/repo#8"),
    )
    assert p.plan(replace(found, capacity=(p.Capacity("claude", "a"),))).waiting[0].reason.code == "pr_open"


def test_facts_from_counts_a_shared_login_once_and_passes_manual_requests():
    meter = Meter("codex", "test", 1000, five_hour_pct=10)
    capacity = [
        p.Capacity("claude", "slot-1"),
        p.Capacity("codex", "shared", meter),
        p.Capacity("claude", "slot-2"),
        p.Capacity("codex", "shared", meter),
    ]
    manual = [{"name": "m", "repo": "example/repo", "issue": "example/repo#1", "files": ["a"]}]
    found = p.facts_from([], [], [], capacity, manual, tracked_paths={"example/repo": {"a"}})
    assert [(c.engine, c.login) for c in found.capacity] == [
        ("claude", "slot-1"),
        ("codex", "shared"),
        ("claude", "slot-2"),
    ]
    assert found.manual_requests == (p.ManualRequest("m", "example/repo", "example/repo#1", ("a",)),)
    assert found.tracked_paths == {"example/repo": {"a"}}


def test_a_slot_with_two_engines_takes_one_launch():
    # QA round 2: one free slot offered on Claude and on the shared Codex login proposed two launches.
    capacity = (p.Capacity("claude", "c1", slot="s1"), p.Capacity("codex", "shared", slot="s1"))
    result = p.plan(replace(facts(item(1, ["a"]), item(2, ["b"])), capacity=capacity))
    assert launched(result) == [1]
    assert result.waiting[0].reason.code == "no_capacity"


def test_slot_entries_survive_the_shared_codex_dedupe():
    capacity = [
        p.Capacity("claude", "c1", slot="s1"),
        p.Capacity("codex", "shared", slot="s1"),
        p.Capacity("claude", "c2", slot="s2"),
        p.Capacity("codex", "shared", slot="s2"),
    ]
    built = p.facts_from([], [], [], capacity, [], default_repo="example/repo", now=1000)
    assert len({entry.slot for entry in built.capacity}) == 2
    two = replace(facts(item(1, ["a"]), item(2, ["b"]), item(3, ["c"])), capacity=built.capacity)
    assert launched(p.plan(two)) == [1, 2]


@pytest.mark.parametrize(
    "change", [{"repo": None}, {"labels": None}, {"labels": ("priority", None)}, {"position": None}]
)
def test_a_malformed_field_never_stops_the_pass(change):
    # QA round 2: these raised outside the per-item guard and lost every decision.
    bad = replace(item(1, ["a"]), **change)
    result = p.plan(facts(bad, item(2, ["b"])))
    assert 2 in launched(result)


def test_owner_precedes_priority_loop_and_only_one_loop_launches():
    result = p.plan(
        facts(
            item(1, ["a"], labels=("loop", "priority")),
            item(2, ["b"]),
            item(3, ["c"], labels=("loop",)),
            loop_enabled=True,
        )
    )
    assert launched(result) == [2, 1]
    assert result.waiting[0].reason.code == "loop_running"


@pytest.mark.parametrize("slots, expected", [(1, []), (2, [2])])
def test_loop_reserves_last_slot_for_waiting_owner(slots, expected):
    run = p.ActiveRun("busy", "example/repo", predicted_files=("a",))
    result = p.plan(
        facts(
            item(1, ["a"]),
            item(2, ["b"], labels=("loop",)),
            slots=slots,
            active_runs=(run,),
            loop_enabled=True,
        )
    )
    assert launched(result) == expected


@pytest.mark.parametrize("enabled,killed", [(False, False), (True, True)])
def test_paused_loop_launches_nothing(enabled, killed):
    result = p.plan(facts(item(1, ["a"], labels=("loop",)), loop_enabled=enabled, loop_killed=killed))
    assert not result.launches
    assert result.waiting[0].reason.code == "loop_paused"


def test_live_loop_run_blocks_disjoint_loop_item():
    run = p.ActiveRun("busy", "example/other", labels={"factory-loop": "issue"})
    result = p.plan(facts(item(1, ["a"], labels=("loop",)), active_runs=(run,), loop_enabled=True))
    assert not result.launches
    assert result.waiting[0].reason.code == "loop_running"


@pytest.mark.parametrize("status", ["In flight", "In review"])
def test_loop_board_work_reserves_iteration_between_build_and_review(status):
    result = p.plan(
        facts(
            item(1, ["a"], labels=("loop",), status=status),
            item(2, ["b"], labels=("loop",)),
            item(3, ["c"]),
            loop_enabled=True,
        )
    )
    assert launched(result) == [3]
    assert result.waiting[0].reason.code == "loop_running"


def test_loop_linked_pr_reserves_iteration_after_project_passes():
    result = p.plan(
        facts(
            item(1, ["a"], labels=("loop",), status="Done", linked_prs=("pr-1",)),
            item(2, ["b"], labels=("loop",)),
            loop_enabled=True,
        )
    )
    assert not result.launches
    assert result.waiting[0].reason.code == "loop_running"


def test_loop_label_is_case_insensitive_for_pause_and_single_flight():
    paused = p.plan(
        facts(item(1, ["a"], labels=("Loop",)), item(2, ["b"], labels=("LOOP",)), loop_enabled=False)
    )
    assert launched(paused) == []
    assert {w.reason.code for w in paused.waiting} == {"loop_paused"}
    one = p.plan(facts(item(1, ["a"], labels=("Loop",)), item(2, ["b"], labels=("lOOp",)), loop_enabled=True))
    assert launched(one) == [1]
    assert one.waiting[0].reason.code == "loop_running"


def test_owner_precedes_a_case_variant_loop_item():
    result = p.plan(facts(item(1, ["a"], labels=("Loop",)), item(2, ["b"]), loop_enabled=True))
    assert launched(result) == [2, 1]


def test_the_authors_preferred_engine_wins_over_headroom():
    # Owner rule 4: headroom only breaks a tie; the author's engine from policy is the preference.
    claude = Meter("claude login", "test", 1000, five_hour_pct=97, five_hour_resets_at=9000)
    codex = Meter("codex", "test", 1000, five_hour_pct=12, five_hour_resets_at=9000)
    capacity = (p.Capacity("claude", "login", claude), p.Capacity("codex", "shared", codex))
    snapshot = replace(facts(item(1, ["a"])), capacity=capacity)
    assert p.plan(snapshot).launches[0].engine == "claude"
    assert p.plan(replace(snapshot, author_engine="codex")).launches[0].engine == "codex"
    # A full meter is a hint, not a limit: both full still launches, on the preferred engine.
    full = tuple(
        replace(c, meter=replace(meter, five_hour_pct=100))
        for c, meter in zip(capacity, (claude, codex), strict=True)
    )
    assert p.plan(replace(snapshot, capacity=full)).launches[0].engine == "claude"
    # A recorded refusal does move it.
    limited = (replace(capacity[0], meter=replace(claude, limited_until=9000)), capacity[1])
    assert p.plan(replace(snapshot, capacity=limited)).launches[0].engine == "codex"


def test_a_ready_item_that_found_no_capacity_launches_when_a_slot_frees():
    ready = item(1, ["a"])
    full = p.plan(facts(ready, slots=0))
    assert [w.reason.code for w in full.waiting] == ["no_capacity"]
    freed = p.plan(facts(ready, slots=1))
    assert launched(freed) == [1] and not freed.waiting


def test_a_parked_item_never_launches_even_with_capacity():
    assert launched(p.plan(facts(item(1, ["a"], status="Backlog"), slots=3))) == []


# --- projects on one repository share the slots ---------------------------------------------------------


def repo_item(number_, files=None, repo="example/engine"):
    return replace(item(number_, files), repo=repo)


@pytest.mark.parametrize("repo", ["example/engine", "example/other"])
def test_a_running_project_does_not_hold_another_project_on_the_same_repo(repo):
    run = p.ActiveRun("repo-run", repo, predicted_files=("src/a.py",))
    result = p.plan(facts(repo_item(1, ["docs/x.md"], repo), active_runs=(run,)))
    assert launched(result) == [1]
    assert not result.waiting


@pytest.mark.parametrize("repo", ["example/engine", "example/other"])
def test_projects_on_one_repo_start_together_while_slots_are_free(repo):
    result = p.plan(facts(repo_item(1, ["a"], repo), repo_item(2, ["b"], repo), repo_item(3, ["c"], repo)))
    assert launched(result) == [1, 2, 3]


def test_projects_on_one_repo_wait_for_a_slot_not_for_each_other():
    result = p.plan(facts(repo_item(1, ["a"]), repo_item(2, ["b"]), slots=1))
    assert launched(result) == [1]
    assert result.waiting[0].reason.code == "no_capacity"


def test_a_finished_run_does_not_hold_the_next_item_on_its_repo():
    run = p.ActiveRun("repo-run", "example/engine", phase="SUCCEEDED", predicted_files=("src/a.py",))
    assert launched(p.plan(facts(repo_item(1, ["src/a.py"]), active_runs=(run,)))) == [1]


@pytest.mark.parametrize("repo", ["example/engine", "example/other"])
def test_overlapping_files_on_one_repo_wait_for_the_running_project(repo):
    run = p.ActiveRun("repo-run", repo, predicted_files=("src/a.py",))
    result = p.plan(facts(repo_item(1, ["src/a.py"], repo), active_runs=(run,)))
    assert launched(result) == []
    assert result.waiting[0].reason.code == "file_conflict"


def test_overlapping_files_in_one_pass_wait_for_the_first_item():
    result = p.plan(facts(repo_item(1, ["src/a.py"]), repo_item(2, ["src/a.py"]), repo_item(3, ["b"])))
    assert launched(result) == [1, 3]
    assert result.waiting[0].reason == p.Reason("file_conflict", "I1")


def test_a_manual_request_waits_while_a_run_of_its_project_is_live():
    manual = p.ManualRequest("alpha", "example/repo", files=("a",))
    other = p.ManualRequest("beta", "example/repo", files=("b",))
    old = p.ActiveRun(
        "alpha-1-0-aaaaaa", "example/repo", predicted_files=("z",), labels={"factory-project": "alpha"}
    )
    result = p.plan(facts(manual_requests=(manual, other), active_runs=(old,)))
    assert [launch.item for launch in result.launches] == [other]
    assert [(w.item, w.reason) for w in result.waiting] == [(manual, p.Reason("project_running", old.name))]
    ended = replace(old, phase="SUCCEEDED")
    assert [
        launch.item for launch in p.plan(facts(manual_requests=(manual,), active_runs=(ended,))).launches
    ] == [manual]
