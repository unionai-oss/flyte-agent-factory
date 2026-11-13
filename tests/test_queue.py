"""The launch queue's storage and dispatch order, against a real local state directory."""

import asyncio
import dataclasses
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flyte_agent_factory import _factory as factory
from flyte_agent_factory import _factory_base, _queue, _timeline


def request(name, gen=1, at=100.0, **kw):
    fields = {
        "name": name,
        "gen": gen,
        "kind": "new",
        "brief": "brief",
        "model": "opus",
        "repo": "r",
        "demo_package": "",
        "policy": {},
    }
    return _queue.Request(**{**fields, "enqueued_at": at, **kw})


def files(queue):
    return sorted(
        str(p.relative_to(queue.removeprefix("file://")))
        for p in Path(queue.removeprefix("file://")).rglob("*")
        if p.is_file()
    )


def fill_slots(world, *names):
    for name, slot in zip(names, _factory_base.SLOTS, strict=False):
        world.add(name, slot)


def test_enqueue_writes_the_request_and_pending_returns_it(queue_state):
    _queue.enqueue(queue_state, request("alpha"))
    assert files(queue_state) == ["alpha/0001.json"]
    [entry] = _queue.pending(queue_state)
    assert entry.request == request("alpha")


def test_pending_is_fifo_by_enqueue_time_then_name(queue_state):
    for r in (request("bravo", at=2), request("charlie", at=1), request("alpha", at=1)):
        _queue.enqueue(queue_state, r)
    assert [e.name for e in _queue.pending(queue_state)] == ["alpha", "charlie", "bravo"]


def test_launching_a_waiting_name_again_keeps_its_place(flyte_world):
    fill_slots(flyte_world, "x", "y")
    first = factory.launch_project("first", "alpha")
    other = factory.launch_project("other", "bravo")
    again = factory.launch_project("second try", "alpha", repo="")
    assert (first["status"], first["position"], other["position"]) == ("queued", 1, 2)
    assert (again["status"], again["gen"], again["position"]) == ("updated", first["gen"], 1)
    [head, _] = _queue.pending(flyte_world.queue)
    assert head.request is not None
    assert (head.name, head.request.brief) == ("alpha", "second try")
    assert files(flyte_world.queue) == ["alpha/0001.json", "bravo/0001.json"]


def test_cancel_writes_the_tombstone_and_unknown_names_are_refused(flyte_world):
    fill_slots(flyte_world, "x", "y")
    factory.launch_project("brief", "alpha")
    assert factory.cancel("alpha") == {"cancelled": [1]}
    assert _queue.pending(flyte_world.queue) == []
    assert "alpha/0001.cancelled" in files(flyte_world.queue)
    with pytest.raises(ValueError, match="'alpha' is not queued"):
        factory.cancel("alpha")
    with pytest.raises(ValueError, match="not queued"):
        factory.cancel("nobody")


def test_a_request_whose_run_exists_is_closed_on_sight(flyte_world):
    fill_slots(flyte_world, "x", "y")
    _queue.enqueue(flyte_world.queue, request("alpha"))
    # a dispatcher died after creating it; the run is found by its labels, whatever its name
    flyte_world.add(
        "elsewhere-run",
        "elsewhere",
        labels={_factory_base.LABEL_PROJECT: "alpha", _factory_base.LABEL_GEN: "1"},
    )
    assert factory.dispatch() == []
    assert _queue.pending(flyte_world.queue) == []
    assert _queue.marker(flyte_world.queue, "alpha", 1, "started")["run"] == "elsewhere-run"


def test_an_unreadable_request_is_an_error_row_and_does_not_block_the_head(flyte_world):
    bad = Path(flyte_world.queue.removeprefix("file://")) / "aaa" / "0001.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not json")
    _queue.enqueue(flyte_world.queue, request("bbb"))
    view = factory.queue_view()
    assert [r.get("position") for r in view["queue"]] == [1, None]
    assert "unreadable request" in view["queue"][1]["error"]
    assert len(factory.dispatch()) == 1  # bbb starts
    assert _queue.marker(flyte_world.queue, "bbb", 1, "started")["run"].startswith("bbb-1-0-")


def test_foreign_objects_do_not_break_the_listing(queue_state):
    stray = Path(queue_state.removeprefix("file://")) / "alpha" / "notes.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("hi")
    _queue.enqueue(queue_state, request("alpha"))
    assert [e.name for e in _queue.entries(queue_state)] == ["alpha"]


def test_run_ids_fit_30_characters_keep_their_random_suffix_and_never_repeat():
    long = "a" * 41
    ids = [_queue.run_id(long, 1234, 7) for _ in range(50)]
    assert (
        len(set(ids)) == 50
    )  # asking twice for the same (name, gen, attempt) must not give the same run name
    for rid in ids:
        assert len(rid) <= 30 and rid[0].isalpha()
        assert re.fullmatch(r"a+-1234-7-[0-9a-z]{6}", rid)
    assert _queue.run_id("ab", 1, 0).startswith("ab-1-0-")
    assert _queue.run_id("9lives", 1, 0)[
        0
    ].isalpha()  # a project name may start with a digit; a run name may not


def test_the_sweep_dispatches_when_a_slot_frees(flyte_world):
    fill_slots(flyte_world, "x", "y")
    factory.launch_project("brief", "alpha")
    assert json.loads(asyncio.run(factory.sweep.func())) == {"dispatched": []}
    flyte_world.runs["x"].phase = "SUCCEEDED"
    swept = json.loads(asyncio.run(factory.sweep.func()))
    [run] = swept["dispatched"]
    assert run.startswith("alpha-1-0-")


def test_the_chat_tasks_are_thin_wrappers(flyte_world):
    fill_slots(flyte_world, "x", "y")
    queued = json.loads(
        asyncio.run(factory.launch.func("brief", "alpha", policy='{"JUDGE_MODEL": "sonnet"}'))
    )
    assert queued["status"] == "queued"
    listed = json.loads(asyncio.run(factory.queue_list.func()))
    assert [r["name"] for r in listed["queue"]] == ["alpha"]
    assert [s["run"] for s in listed["slots"]] == ["x", "y"]
    assert json.loads(asyncio.run(factory.queue_cancel.func("alpha"))) == {"cancelled": [1]}
    assert _queue.pending(flyte_world.queue) == []


# --- the dispatch lease ---------------------------------------------------------------------------


def lease_file(queue):
    return Path(queue.removeprefix("file://")) / ".dispatch-lease.json"


def test_the_lease_goes_to_one_holder_until_it_expires_or_is_released(queue_state):
    assert _queue.take_lease(queue_state, "a", now=1000) is None
    assert _queue.take_lease(queue_state, "b", now=1001) == "a"
    assert _queue.take_lease(queue_state, "a", now=1002) is None  # ours already
    assert (
        _queue.take_lease(queue_state, "b", now=1002 + _queue.LEASE_TTL + 1) is None
    )  # a's (renewed at 1002) expired
    _queue.release_lease(queue_state, "a")  # a no longer holds it: b's lease stays
    assert json.loads(lease_file(queue_state).read_text())["holder"] == "b"
    _queue.release_lease(queue_state, "b")
    assert not lease_file(queue_state).exists()


@pytest.mark.parametrize("junk", ["{not json", "[]", '{"holder": 3}', '{"holder": "x"}'])
def test_a_corrupt_lease_is_absent(queue_state, junk):
    lease_file(queue_state).parent.mkdir(parents=True)
    lease_file(queue_state).write_text(junk)
    assert _queue.take_lease(queue_state, "a", now=1000) is None


def test_the_last_writer_in_the_settle_window_gets_the_lease(queue_state, monkeypatch):
    # two takers both saw no lease; b writes after a, so after the settle a's re-read finds b
    def rival_writes(_seconds):
        lease_file(queue_state).write_text(json.dumps({"holder": "b", "expires": 5000}))

    monkeypatch.setattr(time, "sleep", rival_writes)
    assert _queue.take_lease(queue_state, "a", now=1000) == "b"


def test_the_lease_is_listed_by_nobody(queue_state):
    _queue.take_lease(queue_state, "a", now=1000)
    _queue.enqueue(queue_state, request("alpha"))
    assert [e.name for e in _queue.entries(queue_state)] == ["alpha"]


def test_dispatch_holds_the_lease_while_it_runs_and_releases_it_even_on_error(flyte_world, monkeypatch):
    _queue.enqueue(flyte_world.queue, request("alpha"))
    seen = []

    def boom(*args, **kwargs):
        seen.append(json.loads(lease_file(flyte_world.queue).read_text())["holder"])
        raise RuntimeError("boom")

    monkeypatch.setattr(factory, "launch_on_slot", boom)
    with pytest.raises(RuntimeError):
        factory.dispatch()
    assert len(seen) == 1
    assert not lease_file(flyte_world.queue).exists()


def test_dispatch_does_nothing_while_another_dispatcher_holds_the_lease(flyte_world):
    _queue.enqueue(flyte_world.queue, request("alpha"))
    assert _queue.take_lease(flyte_world.queue, "someone-else") is None
    assert factory.dispatch() == []
    assert flyte_world.created == []
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["alpha"]
    assert json.loads(lease_file(flyte_world.queue).read_text())["holder"] == "someone-else"


def test_when_both_markers_exist_the_later_one_wins(queue_state):
    _queue.enqueue(queue_state, request("alpha"))
    _queue.mark(queue_state, "alpha", 1, "started", run="r1")
    _queue.mark(queue_state, "alpha", 1, "cancelled")  # written later, no abort recorded
    assert _queue.entries(queue_state)[0].closed == "cancelled"
    _queue.enqueue(queue_state, request("bravo"))
    _queue.mark(queue_state, "bravo", 1, "cancelled")
    _queue.mark(queue_state, "bravo", 1, "started", run="r2")  # a live run nothing aborted
    assert [e.closed for e in _queue.entries(queue_state, "bravo")] == ["started"]
    _queue.enqueue(queue_state, request("charlie"))
    _queue.mark(queue_state, "charlie", 1, "cancelled", aborted_run="r3")
    _queue.mark(queue_state, "charlie", 1, "started", run="r3")  # started after, but cancel aborted that run
    assert [e.closed for e in _queue.entries(queue_state, "charlie")] == ["cancelled"]


@pytest.mark.parametrize(
    "field",
    [
        {"policy": {"AGENT_FACTORY_X": 1}},
        {"policy": ["x"]},
        {"demo_package": 3},
        {"model": 4},
        {"base": 5},
        {"resume_from": 6},
        {"kind": "other"},
    ],
)
def test_a_request_with_a_field_of_the_wrong_type_is_not_a_request(field):
    body = json.loads(request("alpha").to_json())
    body.update(field)
    with pytest.raises(ValueError, match="not a launch request"):
        _queue.Request.from_json(json.dumps(body).encode())


@pytest.mark.parametrize("body", [request("alpha", gen=1), request("bravo", gen=2)])
def test_a_request_whose_body_disagrees_with_its_path_is_closed_failed_as_corrupt(flyte_world, body):
    path = Path(flyte_world.queue.removeprefix("file://")) / "bravo" / "0001.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(body.to_json())  # a bravo/0001 file that says it is something else
    _queue.enqueue(flyte_world.queue, request("charlie", at=200))
    assert [run.split("-")[0] for run in factory.dispatch()] == ["charlie"]
    assert "corrupt" in _queue.marker(flyte_world.queue, "bravo", 1, "failed")["error"]
    assert [(e.name, e.closed) for e in _queue.entries(flyte_world.queue)] == [
        ("bravo", "failed"),
        ("charlie", "started"),
    ]
    assert flyte_world.created[0]["inputs"]["name"] == "charlie"
    assert not (path.parent / "0001.started").exists() and not (path.parent / "0001.cancelled").exists()


def test_a_corrupt_request_alone_is_still_closed(flyte_world):
    path = Path(flyte_world.queue.removeprefix("file://")) / "bravo" / "0001.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(request("alpha").to_json())
    assert factory.dispatch() == []
    assert _queue.entries(flyte_world.queue)[0].closed == "failed"


def test_labels_are_read_from_a_real_flyte_run():
    # The fake must not be kinder than the SDK: a real Run carries labels only on its protobuf.
    from flyte import remote
    from flyteidl2.workflow import run_definition_pb2  # type: ignore[import-untyped]  # ships no stubs

    from flyte_agent_factory import _factory_base

    pb = run_definition_pb2.Run()
    pb.action.SetInParent()
    pb.labels["factory-project"] = "alpha"
    run = remote.Run(pb2=pb)
    assert not hasattr(run, "labels")
    assert _factory_base.run_labels(run) == {"factory-project": "alpha"}


# --- chat status ---------------------------------------------------------------------------------


def status_result(name, rows=40):
    return json.loads(asyncio.run(factory.status.func(name, rows)))


def status_run(world, name, project="alpha", phase="RUNNING"):
    from flyte_agent_factory import _dashboard

    run = world.add(name, _factory_base.SLOTS[0], phase=phase, labels={_factory_base.LABEL_PROJECT: project})
    run.report = _dashboard.status_script(
        {
            "generated_at": 1000,
            "project": project,
            "phase": {"kind": "review", "round": 2, "max_rounds": 5},
            "activity": "reviewing",
            "timeline": [{"time": i, "icon": "🔧", "text": f"event {i}"} for i in range(80)],
            "needs_you": [{"text": "approve?", "timed_out": False}],
            "review": {"scores": [5, 3], "open": ["race in dispatch"], "converging": True, "rulings": 0},
            "deliverables": {"pr": "https://github.example/pull/1"},
            "cost": {"meters": [{"account": "slot", "used_pct": 20}]},
            "private_extra": "must not be returned",
        }
    )
    return run


def test_status_prefers_a_live_project_run_over_a_newer_ended_run(flyte_world, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1042)
    live = status_run(flyte_world, "live")
    status_run(flyte_world, "ended", phase="SUCCEEDED")
    result = status_result("alpha")
    assert (result["name"], result["run"], result["url"]) == ("alpha", live.name, live.url)
    assert result["run_phase"] == "RUNNING"
    assert result["phase"] == "review round 2 of 5"
    assert result["activity"] == "reviewing"
    assert result["snapshot_age_s"] == 42
    assert result["open_questions"] == [{"text": "approve?", "timed_out": False}]
    assert result["review"]["round"] == 2 and result["review"]["max_rounds"] == 5
    assert result["review"] == {
        "round": 2,
        "max_rounds": 5,
        "scores": [5, 3],
        "open": ["race in dispatch"],
        "converging": True,
        "rulings": 0,
    }
    assert result["pr_url"] == "https://github.example/pull/1"
    assert result["meters"] == [{"account": "slot", "used_pct": 20}]
    assert "private_extra" not in result
    assert len(result["timeline"]) == 40
    assert result["timeline"][0]["text"] == "event 40"
    assert result["timeline"][-1]["text"] == "event 79"
    assert flyte_world.created == []


def test_status_uses_the_newest_ended_run_and_accepts_a_run_id(flyte_world):
    status_run(flyte_world, "old", phase="SUCCEEDED")
    latest = status_run(flyte_world, "new", phase="FAILED")
    assert status_result("alpha")["run"] == latest.name
    assert status_result("old")["run"] == "old"
    assert status_result("old")["name"] == "alpha"


def test_status_finds_a_live_run_created_before_project_labels(flyte_world):
    run = status_run(flyte_world, "legacy")
    run.pb2.labels.clear()
    run.inputs_ = {"name": "alpha"}
    status_run(flyte_world, "ended", phase="SUCCEEDED")
    assert status_result("alpha")["run"] == run.name


@pytest.mark.parametrize("rows, count", [(3, 3), (0, 0), (-1, 0), (1000, 80)])
def test_status_bounds_the_requested_rows(flyte_world, rows, count):
    status_run(flyte_world, "run")
    result = status_result("alpha", rows)
    assert len(result["timeline"]) == count
    if count:
        assert result["timeline"][-1]["text"] == "event 79"


def test_status_drops_oldest_timeline_rows_to_fit_the_size_budget(flyte_world):
    from flyte_agent_factory import _dashboard

    run = status_run(flyte_world, "run")
    data = _dashboard.from_report(run.report)
    assert data is not None
    for row in data["timeline"]:
        row["text"] += "x" * 2000
    run.report = _dashboard.status_script(data)
    output = asyncio.run(factory.status.func("alpha", 80))
    result = json.loads(output)
    assert len(output) <= 40_000
    assert 0 < len(result["timeline"]) < 80
    assert result["timeline"][-1]["text"].startswith("event 79")


@pytest.mark.parametrize(
    "page",
    ["", None, "<p>old report</p>", '<script type="application/json" id="factory-status">bad</script>'],
)
def test_status_explains_an_absent_snapshot_with_the_run_url(flyte_world, page):
    run = status_run(flyte_world, "run")
    run.report = page
    result = status_result("alpha")
    assert result["error"] and result["url"] == run.url
    assert "timeline" not in result


def test_status_explains_a_report_fetch_failure_without_exposing_the_exception(flyte_world, monkeypatch):
    run = status_run(flyte_world, "run")

    def unavailable():
        raise RuntimeError("sensitive transport details")

    monkeypatch.setattr(run, "get_report", unavailable)
    result = status_result("alpha")
    assert result["url"] == run.url and "report" in result["error"]
    assert "sensitive" not in json.dumps(result)


def test_status_unknown_project_is_an_error(flyte_world):
    assert "error" in status_result("unknown")


def test_status_accepts_an_older_snapshot_without_optional_fields(flyte_world):
    from flyte_agent_factory import _dashboard

    run = status_run(flyte_world, "run")
    run.report = _dashboard.status_script({"phase": {"kind": "building"}})
    result = status_result("alpha")
    assert result["phase"] == "building"
    assert result["timeline"] == [] and result["open_questions"] == []
    assert result["review"] == {} and result["snapshot_age_s"] is None


def test_status_keeps_the_size_cap_when_the_summary_alone_is_too_large(flyte_world):
    from flyte_agent_factory import _dashboard

    run = status_run(flyte_world, "run")
    run.report = _dashboard.status_script({"needs_you": [{"text": "q" * 50_000}]})
    result = status_result("alpha")
    assert "error" in result and result["url"] == run.url
    assert len(json.dumps(result)) <= 40_000


@pytest.mark.parametrize("configured", [True, False, None])
def test_status_preserves_valid_standards_configuration(flyte_world, configured):
    from flyte_agent_factory import _dashboard

    run = status_run(flyte_world, "configured")
    standards = {"configured": configured}
    run.report = _dashboard.status_script({"standards": {**standards, "extra": {"nested": "unused"}}})
    assert status_result("alpha", 0)["standards"] == standards


def test_status_does_not_disguise_lookup_permission_errors_as_unknown_names(flyte_world, monkeypatch):
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError
    from flyte import remote

    def denied(name):
        raise ConnectError(Code.PERMISSION_DENIED, "denied")

    monkeypatch.setattr(remote.Run, "get", denied)
    with pytest.raises(ConnectError) as error:
        status_result("unknown")
    assert error.value.code == Code.PERMISSION_DENIED


def test_status_reads_a_snapshot_the_supervisor_really_writes(flyte_world, monkeypatch):
    # qa: the review fields were read from a hand-written fixture with a key the supervisor never writes.

    timeline = _timeline._Timeline()
    timeline.row("❌", "correctness: FAIL")
    run = flyte_world.add("real", _factory_base.SLOTS[0], labels={_factory_base.LABEL_PROJECT: "alpha"})
    run.report = timeline.render()  # the page the supervisor publishes, snapshot and rows included
    review = status_result("alpha")["review"]
    assert {"scores", "converging", "rulings", "open"} <= set(review)
    assert review["verdicts"] == {"correctness": "FAIL"}


def test_status_verdicts_are_the_newest_round_only(flyte_world):
    # qa: a judge's PASS from an earlier round was reported as current after it failed the next round.

    timeline = _timeline._Timeline()
    timeline.row("🔍", "QA round 1: 3 reviewers")
    timeline.row("✅", "correctness: PASS")
    timeline.row("❌", "completeness: FAIL")
    timeline.row("🔍", "QA round 2: 3 reviewers")
    timeline.row("⚠️", "correctness: no verdict, treating as FAIL")
    run = flyte_world.add("rounds", _factory_base.SLOTS[0], labels={_factory_base.LABEL_PROJECT: "alpha"})
    run.report = timeline.render()
    review = status_result("alpha")["review"]
    assert review["verdicts"] == {"correctness": "FAIL"}
    assert review["verdicts_round"] == 2


def test_status_reads_the_timeline_of_a_report_from_before_the_json_timeline(flyte_world):
    # Owner (inbox 019): factory.status returned an empty timeline and activity for a run whose supervisor
    # predates the JSON timeline; the rows were only in the page's HTML table.
    from flyte_agent_factory import _dashboard

    old_table = (
        "<details><summary>Timeline (3)</summary><table>"
        "<tr><td><code>10:00:03</code></td><td>📝</td><td>DONE &amp; ready</td></tr>"
        "<tr><td><code>10:00:02</code></td><td>❌</td><td>craft: FAIL</td></tr>"
        "<tr><td><code>10:00:01</code></td><td>🔍</td><td>QA round 2: 3 reviewers</td></tr>"
        "</table></details>"
        "<details><summary>Routine (1)</summary><table>"
        "<tr><td><code>09:59:00</code></td><td>💾</td><td>checkpoint</td></tr></table></details>"
    )
    page = (
        "<p><b>Status:</b> fixing review findings</p>"
        + old_table
        + _dashboard.status_script({"generated_at": 1000, "project": "alpha", "phase": {"kind": "building"}})
    )
    run = flyte_world.add("old", _factory_base.SLOTS[0], labels={_factory_base.LABEL_PROJECT: "alpha"})
    run.report = page
    result = status_result("alpha")
    assert result["activity"] == "fixing review findings"
    assert [row["text"] for row in result["timeline"]] == [
        "QA round 2: 3 reviewers",
        "craft: FAIL",
        "DONE & ready",
    ]
    assert result["review"]["verdicts"] == {"craft": "FAIL"} and result["review"]["verdicts_round"] == 2
    assert [(row["time"], row["clock"]) for row in result["timeline"]] == [
        (None, "10:00:01"),
        (None, "10:00:02"),
        (None, "10:00:03"),
    ]


def test_status_rows_from_the_json_timeline_carry_a_utc_clock(flyte_world, monkeypatch):
    import time as _time

    # A non-UTC zone, so a local-time clock would differ from the UTC one (the pod itself runs in UTC).
    monkeypatch.setenv("TZ", "America/New_York")
    _time.tzset()
    monkeypatch.setattr(_time, "time", lambda: 1042)
    status_run(flyte_world, "live")
    rows = status_result("alpha")["timeline"]
    assert rows and isinstance(rows[-1]["time"], (int, float))
    expected = datetime.fromtimestamp(rows[-1]["time"], tz=timezone.utc).strftime("%H:%M:%S")
    try:
        assert rows[-1]["clock"] == expected
    finally:
        monkeypatch.undo()
        _time.tzset()


def test_listing_returns_uris_the_store_reads_back(state_uri, tmp_path):
    """Object-store listings drop the scheme; a path without it would reach the local filesystem."""
    prefix = f"{state_uri}/outbox"
    for name in ("2.json", "1.json", "notes.txt", "nested/3.json"):
        _queue._write(f"{prefix}/{name}", name.encode())
    found = _queue.listing(prefix)
    assert found == [f"{prefix}/1.json", f"{prefix}/2.json", f"{prefix}/nested/3.json"]
    assert [_queue._fs(path).cat_file(path) for path in found] == [b"1.json", b"2.json", b"nested/3.json"]
    assert _queue.listing(f"{state_uri}/nothing-here") == []


def test_listing_keeps_a_plain_local_path_plain(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    assert _queue.listing(str(tmp_path)) == [str(tmp_path / "a.json")]


def test_entries_read_an_object_store(state_uri, monkeypatch):
    monkeypatch.setattr(_queue, "LEASE_SETTLE", 0)
    queue = _queue.root(state_uri)
    _queue.enqueue(queue, request("alpha"))
    _queue.enqueue(queue, request("beta"))
    _queue.mark(queue, "beta", 1, "started", run="beta-1-0-abcdef")
    assert [(e.name, e.closed) for e in _queue.entries(queue)] == [("alpha", ""), ("beta", "started")]
    assert [e.request.name for e in _queue.pending(queue) if e.request] == ["alpha"]


# --- projects on one repository share the slots --------------------------------------------------------

ENGINE = "git@example.org:team/engine.git"
OTHER = "git@example.org:team/other.git"


@pytest.fixture
def engine_repo(monkeypatch):
    profile = _factory_base.PROFILE
    monkeypatch.setattr(
        _factory_base, "PROFILE", dataclasses.replace(profile, repos={**profile.repos, "factory": ENGINE})
    )


@pytest.mark.parametrize("repo", [ENGINE, OTHER])
def test_a_launch_starts_beside_a_running_project_on_its_repo(flyte_world, engine_repo, repo):
    flyte_world.add("running", _factory_base.SLOTS[0], inputs={"repo": repo})
    _queue.enqueue(flyte_world.queue, request("alpha", repo=repo))
    assert [run.split("-")[0] for run in factory.dispatch()] == ["alpha"]


@pytest.mark.parametrize("repo", [ENGINE, OTHER])
def test_projects_on_one_repo_start_in_one_sweep_while_slots_are_free(flyte_world, engine_repo, repo):
    _queue.enqueue(flyte_world.queue, request("alpha", at=1, repo=repo))
    _queue.enqueue(flyte_world.queue, request("bravo", at=2, repo=repo))
    assert [run.split("-")[0] for run in factory.dispatch()] == ["alpha", "bravo"]
    assert _queue.pending(flyte_world.queue) == []


def test_projects_on_one_repo_wait_for_a_slot_and_start_when_it_frees(flyte_world, engine_repo, capsys):
    for slot, name in zip(_factory_base.SLOTS, ("one", "two"), strict=False):
        flyte_world.add(name, slot, inputs={"repo": ENGINE})
    _queue.enqueue(flyte_world.queue, request("alpha", repo=ENGINE))
    assert factory.dispatch() == []
    assert "engine_busy" not in capsys.readouterr().out
    flyte_world.runs["one"].phase = "SUCCEEDED"
    assert [run.split("-")[0] for run in factory.dispatch()] == ["alpha"]


# --- a running project keeps the engine it started on ---------------------------------------------------


def test_a_deploy_does_not_change_the_engine_of_a_running_project(flyte_world):
    slot = _factory_base.SLOTS[0]
    first = flyte_world.runs[factory.launch_project("brief", "alpha")["run"]]
    assert first.task_version == "v1"
    flyte_world.deploy(slot)  # the engine is redeployed while alpha runs
    assert first.task_version == "v1"  # a run is bound to the task version it was created from
    second = flyte_world.runs[factory.launch_project("brief", "bravo")["run"]]
    assert (second.task, second.task_version) == (
        _factory_base.SLOTS[1],
        "v1",
    )  # another slot, not redeployed
    first.phase = "SUCCEEDED"
    third = flyte_world.runs[factory.launch_project("brief", "charlie")["run"]]
    assert (third.task, third.task_version) == (slot, "v2")  # a new launch resolves the new deploy
    assert first.task_version == "v1"


@pytest.mark.parametrize("rows", [0, 1, 40])
def test_status_keeps_standards_diagnostics_without_timeline_notices(flyte_world, rows):
    from flyte_agent_factory import _dashboard

    run = status_run(flyte_world, "standards")
    data = _dashboard.from_report(run.report)
    assert data is not None
    data["standards"] = {"configured": False, "warning": "no standards configured"}
    run.report = _dashboard.status_script(data)
    result = status_result("alpha", rows)
    assert result["standards"] == data["standards"]
    assert all("no standards configured" not in row["text"] for row in result["timeline"])


def test_status_marks_missing_standards_metadata_as_unknown(flyte_world):
    status_run(flyte_world, "older")
    result = status_result("alpha", 0)
    assert result["standards"]["configured"] is None
    assert "no standards configured" not in json.dumps(result)


def test_status_keeps_standards_diagnostics_when_summary_exceeds_size_budget(flyte_world):
    from flyte_agent_factory import _dashboard

    run = status_run(flyte_world, "large")
    standards = {"configured": False, "warning": "no standards configured"}
    run.report = _dashboard.status_script({"standards": standards, "needs_you": [{"text": "q" * 50_000}]})
    result = status_result("alpha", rows=0)
    assert result["standards"] == standards
    assert "error" in result and len(json.dumps(result)) <= 40_000


@pytest.mark.parametrize("oversized_summary", [False, True])
def test_status_bounds_standards_metadata(flyte_world, oversized_summary):
    from flyte_agent_factory import _dashboard

    run = status_run(flyte_world, "large")
    run.report = _dashboard.status_script(
        {
            "standards": {
                "configured": False,
                "warning": "no standards configured: " + "x" * 50_000,
                "extra": {"nested": "x" * 50_000},
            },
            "needs_you": [{"text": "q" * 50_000}] if oversized_summary else [],
        }
    )
    output = asyncio.run(factory.status.func("alpha", 0))
    result = json.loads(output)
    assert len(output) <= 40_000
    assert result["standards"]["configured"] is False
    assert result["standards"]["warning"].startswith("no standards configured")
    assert len(result["standards"]["warning"]) <= 512
    assert set(result["standards"]) == {"configured", "warning"}


@pytest.mark.parametrize(
    "metadata",
    [
        False,
        [],
        "invalid",
        42,
        {},
        {"configured": "false"},
        {"configured": 0},
        {"configured": False, "warning": {"nested": "invalid"}},
    ],
)
def test_status_marks_malformed_standards_metadata_as_unknown(flyte_world, metadata):
    from flyte_agent_factory import _dashboard

    run = status_run(flyte_world, "malformed")
    run.report = _dashboard.status_script({"standards": metadata})
    result = status_result("alpha", 0)
    assert result["standards"]["configured"] is None
    assert "invalid" in result["standards"]["warning"]
    assert len(json.dumps(result)) <= 40_000
