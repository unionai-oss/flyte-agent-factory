import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte
import flyte.errors
import pytest
from flyte import remote
from flyte.io import Dir

from flyte_agent_factory import (
    _deploy,
    _factory_base,
    _factory_meters,
    _factory_policy,
    _factory_recreate,
    _meter,
    _queue,
)
from flyte_agent_factory import _factory as factory


def slotted(name, task):
    return SimpleNamespace(
        name=name, phase="RUNNING", to_dict=lambda: {"action": {"metadata": {"task": {"id": {"name": task}}}}}
    )


def test_slots_and_preference():
    assert _factory_meters.free_slot([], _factory_base.SLOTS[1]) == _factory_base.SLOTS[1]
    assert (
        _factory_meters.free_slot([slotted("one", _factory_base.SLOTS[1])], _factory_base.SLOTS[1])
        == _factory_base.SLOTS[0]
    )
    assert _factory_meters.free_slot([slotted(str(i), s) for i, s in enumerate(_factory_base.SLOTS)]) is None


@pytest.mark.parametrize("name", ["x", "Hello", "a/b", "a" * 42, "-bad"])
def test_invalid_names(name):
    with pytest.raises(ValueError, match="name"):
        _factory_policy.validate(name, "opus")


@pytest.mark.parametrize("model", ["", "--opus", "opus; hi", "a/b"])
def test_invalid_models(model):
    with pytest.raises(ValueError, match="model"):
        _factory_policy.validate("valid-name", model)


def test_full_slots_queue_the_launch_instead_of_refusing(flyte_world):
    # Replaces test_full_names_the_live_runs, which pinned the refusal "refusing to launch: live projects".
    flyte_world.add("one", _factory_base.SLOTS[0])
    flyte_world.add("two", _factory_base.SLOTS[1])
    result = factory.launch_project("brief", "valid-name")
    assert (result["status"], result["position"]) == ("queued", 1)
    assert flyte_world.created == []
    assert [s["run"] for s in factory.queue_view()["slots"]] == ["one", "two"]


def test_launch_kwargs(monkeypatch):
    got: dict[str, Any] = {}
    monkeypatch.setattr(factory, "operated_live", list)
    monkeypatch.setattr(remote.Task, "get", lambda task, **kw: got.update(task=task, lookup=kw) or "task")

    def context(**kw):
        got["context"] = kw
        return SimpleNamespace(
            run=lambda task, **inputs: got.update(inputs=inputs) or SimpleNamespace(name="r", url="url")
        )

    monkeypatch.setattr(flyte, "with_runcontext", context)
    result = factory.launch_on_slot(
        _factory_base.SLOTS[0], "write {name}/app", "my-app", model="sonnet", demo="demo", repo="repo"
    )
    assert (result.name, result.url) == ("r", "url")
    assert got["task"] == _factory_base.SLOTS[0]
    assert got["lookup"] == {"auto_version": "latest"}
    assert got["context"] == {"mode": "remote", "project": factory.PROJECT, "domain": factory.DOMAIN}
    assert got["inputs"] == {
        "brief": "write my-app/app",
        "name": "my-app",
        "model": "sonnet",
        "browser": True,
        "demo_package": "demo",
        "repo": "repo",
    }


def test_waiting_project_holds_slot(monkeypatch):
    waiting = slotted("waiting", _factory_base.SLOTS[0])
    waiting.phase = "WAITING_FOR_RESOURCES"
    unrelated = slotted("supervisor", "agent-own.project")
    finished = slotted("finished", _factory_base.SLOTS[1])
    finished.phase = "SUCCEEDED"
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: [waiting, unrelated, finished])
    assert factory.operated_live() == [waiting]
    assert _factory_meters.free_slot(factory.operated_live()) == _factory_base.SLOTS[1]


@pytest.mark.parametrize("demo", ["../demo", "x", "demo;bad"])
def test_invalid_demo(monkeypatch, demo):
    monkeypatch.setattr(factory, "operated_live", lambda: pytest.fail("must validate before listing"))
    with pytest.raises(ValueError, match="slug"):
        factory.launch_project("brief", "valid-name", demo=demo)


def test_long_brief_is_refused(monkeypatch):
    monkeypatch.setattr(factory, "operated_live", lambda: pytest.fail("must validate before listing"))
    with pytest.raises(ValueError, match="brief must be at most"):
        factory.launch_project("a" * (factory.MAX_BRIEF + 1), "valid-name")


def test_factory_env_holds_the_queue_tasks_and_the_meter_task():
    # Chat starts projects through factory.launch (the queue); the plain function is `launch_project`
    # because a task is named after its function. Its one secret is the work token, mounted only so the
    # meter task can fingerprint the account it reports on.
    assert factory.env.name == "factory"
    assert set(factory.env.tasks) == {
        "factory.launch",
        "factory.browser_link",
        "factory.credential_link",
        "factory.credentials",
        "factory.queue_list",
        "factory.queue_cancel",
        "factory.status",
        "factory.set_reserve",
        "factory.get_reserve",
        "factory.rotate_token",
        "factory.sweep",
        "factory.read_login_meter",
        "factory.plan_pass",
        "factory.garden_weekly",
    }
    assert [(s.key, s.as_env_var) for s in cast(list[flyte.Secret], factory.env.secrets)] == [
        (_factory_base.PROFILE.secrets.claude_token, _factory_meters.TOKEN_ENV)
    ]


@pytest.mark.parametrize("own_newer", [True, False])
def test_concurrent_launch_aborts_newer(monkeypatch, own_newer):
    # The own-oldest case used to pin "the oldest run keeps the slot", which let a launcher abort a run it
    # did not create (QA: during a handover that aborted the project holding the slot). Now a launcher
    # yields to any other live run on the slot, whichever is older, and aborts only itself.
    aborted = []
    own = slotted("own", _factory_base.SLOTS[0])
    other = slotted("other", _factory_base.SLOTS[0])
    for run in (own, other):
        run.abort = lambda reason, name=run.name: aborted.append(name)
    order = [own, other] if own_newer else [other, own]
    listings = iter([order])  # the first listing moved to dispatch
    monkeypatch.setattr(factory, "operated_live", lambda: next(listings))
    monkeypatch.setattr(remote.Task, "get", lambda *a, **kw: "task")
    monkeypatch.setattr(factory, "run_project", lambda *a, **kw: own)
    with pytest.raises(ValueError, match="concurrent launch"):
        factory.launch_on_slot(_factory_base.SLOTS[0], "brief", "valid-name")
    assert aborted == ["own"]


def test_concurrent_launch_not_yet_listed_loses_to_a_listed_run(monkeypatch):
    aborted = []
    own = slotted("own", _factory_base.SLOTS[0])
    other = slotted("other", _factory_base.SLOTS[0])
    for run in (own, other):
        run.abort = lambda reason, name=run.name: aborted.append(name)
    listings = iter([[other]])  # the first listing moved to dispatch
    monkeypatch.setattr(factory, "operated_live", lambda: next(listings))
    monkeypatch.setattr(remote.Task, "get", lambda *a, **kw: "task")
    monkeypatch.setattr(factory, "run_project", lambda *a, **kw: own)
    with pytest.raises(ValueError, match="already holds it"):
        factory.launch_on_slot(_factory_base.SLOTS[0], "brief", "valid-name")
    assert aborted == ["own"]


def test_launcher_can_read_slot_interfaces_with_dir_inputs():
    # launch_on_slot reads the slot task's interface remotely; `workspace_in: Dir | None` needs the Dir
    # transformer registered in the launching process (factory-mcp), which only importing flyte.io does.
    code = (
        "from flyte_agent_factory import _factory\n"
        "from flyte.types import TypeEngine\n"
        "from flyteidl2.core import types_pb2 as t\n"
        "dir_or_none = t.LiteralType(union_type=t.UnionType(variants=[\n"
        "    t.LiteralType(blob=t.BlobType(dimensionality=t.BlobType.MULTIPART), "
        "structure=t.TypeStructure(tag='Dir')),\n"
        "    t.LiteralType(simple=t.NONE, structure=t.TypeStructure(tag='none'))]))\n"
        "TypeEngine.guess_python_type(dir_or_none)\n"
    )
    env = {**os.environ, "PYTHONPATH": "src", "AGENT_FACTORY_PROFILE": "example"}
    result = subprocess.run(
        [sys.executable, "-c", code], check=False, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_no_launch_path_takes_unattended():
    """Every project runs as unattended once did; a caller passing it must fail, not run in a wrong mode."""
    import inspect

    for fn in (factory.run_project, factory.launch_on_slot, factory.launch_project):
        assert "unattended" not in inspect.signature(fn).parameters


# --- the launch queue: validation, dispatch, resume -----------------------------------------------


def run_of(world, name, gen=1):
    """The run the queue started for (name, gen): run names are random, the started marker has it."""
    return world.runs[_queue.marker(world.queue, name, gen, "started")["run"]]


def projects(world: Any, run_names: list[str]) -> list[str]:
    """The project names of dispatched runs, in order."""
    return [world.runs[n].pb2.labels[_factory_base.LABEL_PROJECT] for n in run_names]


def add_labelled(world: Any, name: str, project: str, slot: Any, gen: int = 1, **kw: Any) -> Any:
    return world.add(
        name, slot, labels={_factory_base.LABEL_PROJECT: project, _factory_base.LABEL_GEN: str(gen)}, **kw
    )


def written(queue):
    root = Path(queue.removeprefix("file://"))
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "x"}, "name must be a short lowercase slug"),
        ({"name": "Hello"}, "name must be"),
        ({"name": "a" * 42}, "name must be"),
        ({"model": "a/b"}, "model must be a model name"),
        ({"demo": "../demo"}, "slug"),
        ({"policy": {"bad-key": "v"}}, "uppercase policy key"),
        ({"policy": {"PROFILE": "v"}}, "not starting with PROFILE"),
        ({"repo": "git@example.org:evil/repo.git"}, "not allowed.*work-sandbox"),
    ],
)
def test_a_bad_launch_is_refused_with_a_clear_message_and_writes_nothing(flyte_world, kwargs, message):
    with pytest.raises(ValueError, match=message):
        factory.launch_project("brief", **{"name": "valid-name", **kwargs})
    assert written(flyte_world.queue) == [] and flyte_world.created == []


def test_an_empty_repo_means_the_first_allowed_one(flyte_world):
    factory.launch_project("brief", "valid-name")
    [created] = flyte_world.created
    assert created["inputs"]["repo"] == next(iter(factory.allowed_repos().values()))


def test_a_launch_without_state_uri_says_so(monkeypatch):
    with pytest.raises(ValueError, match=r"\[state\] uri"):
        factory.launch_project("brief", "valid-name")


def test_a_resume_cannot_change_repo_or_outgrow_the_brief(flyte_world, monkeypatch):
    monkeypatch.setattr(factory, "allowed_repos", lambda: {"project": "repo-a", "other": "repo-b"})
    monkeypatch.setattr(factory, "MAX_BRIEF", 100)
    factory.launch_project("a" * 50, "proj", repo="repo-a")
    run_of(flyte_world, "proj").phase = "SUCCEEDED"
    with pytest.raises(ValueError, match="must stay 'repo-a'"):
        factory.launch_project("more", "proj", repo="repo-b")
    with pytest.raises(ValueError, match="brief plus the new direction must be at most 100"):
        factory.launch_project("b" * 60, "proj")
    assert factory.launch_project("more", "proj")["kind"] == "resume"  # repo inherited


_NEW_REPO = "git@github.com:team/new-name.git"
_OLD_REPO = "git@github.com:team/old-name.git"


@pytest.fixture
def renamed_repo(monkeypatch):
    """The `other` repo was renamed: the profile has its new URL, with the old one as an alias."""
    profile = dataclasses.replace(
        _factory_base.PROFILE,
        repos={**_factory_base.PROFILE.repos, "other": _NEW_REPO},
        repo_aliases={"other": (_OLD_REPO,)},
    )
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setattr(factory, "allowed_repos", lambda: {"project": "repo-a", "other": _NEW_REPO})


def _started_on_old_repo(flyte_world, name):
    """A project whose last generation started, before the rename, on the old URL."""
    _queue.enqueue(flyte_world.queue, _queue.Request(name, 1, "new", "b", "opus", _OLD_REPO, "", {}, 1.0))
    factory.dispatch()
    run_of(flyte_world, name).phase = "SUCCEEDED"


@pytest.mark.usefixtures("renamed_repo")
def test_a_request_queued_on_a_renamed_repos_old_url_launches_on_the_new_one(flyte_world):
    _queue.enqueue(flyte_world.queue, _queue.Request("old", 1, "new", "b", "opus", _OLD_REPO, "", {}, 1.0))
    assert projects(flyte_world, factory.dispatch()) == ["old"]
    [created] = flyte_world.created
    assert created["inputs"]["repo"] == _NEW_REPO


@pytest.mark.usefixtures("renamed_repo")
@pytest.mark.parametrize("repo", ["", _NEW_REPO, _OLD_REPO, "https://github.com/team/old-name"])
def test_a_resume_of_a_project_on_a_renamed_repo_stays_on_it_under_its_new_url(flyte_world, repo):
    _started_on_old_repo(flyte_world, "proj")
    result = factory.launch_project("more", "proj", repo=repo)
    assert result["kind"] == "resume"
    assert flyte_world.created[-1]["inputs"]["repo"] == _NEW_REPO


@pytest.mark.usefixtures("renamed_repo")
def test_a_resume_of_a_project_on_a_renamed_repo_still_cannot_move_to_another_repo(flyte_world):
    _started_on_old_repo(flyte_world, "proj")
    with pytest.raises(ValueError, match=f"must stay {_NEW_REPO!r}"):
        factory.launch_project("more", "proj", repo="repo-a")
    with pytest.raises(ValueError, match="not allowed"):
        factory.launch_project("more", "proj", repo="git@github.com:team/unrelated.git")


def test_a_name_with_a_live_run_is_pointed_at_its_direct_inbox(flyte_world):
    flyte_world.add("p1-proj-abcd", _factory_base.SLOTS[0], labels={_factory_base.LABEL_PROJECT: "proj"})
    with pytest.raises(ValueError, match="p1-proj-abcd.*direct-"):
        factory.launch_project("brief", "proj")
    assert written(flyte_world.queue) == []


def test_a_free_slot_starts_the_project_at_once(flyte_world):
    result = factory.launch_project(
        "write {name}/app", "my-app", "sonnet", demo="demo", policy={"JUDGE_MODEL": "x"}
    )
    rid = result["run"]
    assert re.fullmatch(r"my-app-1-0-[0-9a-z]{6}", rid)
    slot = _factory_base.SLOTS[0]
    assert result == {
        "name": "my-app",
        "gen": 1,
        "kind": "new",
        "status": "started",
        "run": rid,
        "url": flyte_world.runs[rid].url,
        "slot": slot,
    }
    [created] = flyte_world.created
    assert created["task"] == slot
    assert created["context"] == {
        "mode": "remote",
        "project": factory.PROJECT,
        "domain": factory.DOMAIN,
        "env_vars": {"AGENT_FACTORY_JUDGE_MODEL": "x"},
        "name": rid,
        "labels": {"factory-project": "my-app", "factory-gen": "1"},
    }
    assert created["inputs"] == {
        "brief": "write my-app/app",
        "name": "my-app",
        "model": "sonnet",
        "browser": True,
        "demo_package": "demo",
        "repo": next(iter(factory.allowed_repos().values())),
    }
    assert written(flyte_world.queue) == ["my-app/0001.json", "my-app/0001.started"]


@pytest.mark.parametrize("cap", [2, 1])
def test_no_free_slot_queues_with_its_position_and_creates_no_run(flyte_world, monkeypatch, cap):
    flyte_world.add("one", _factory_base.SLOTS[0])
    if cap == 2:
        flyte_world.add("two", _factory_base.SLOTS[1])
    else:
        monkeypatch.setattr(_factory_base, "cap", lambda: 1)  # OPERATE_CAP=1 with one live run
    result = factory.launch_project("brief", "alpha")
    assert (result["status"], result["position"]) == ("queued", 1)
    assert flyte_world.created == []


def test_fifo_holds_when_a_slot_frees_during_a_launch(flyte_world):
    flyte_world.add("one", _factory_base.SLOTS[0])
    flyte_world.add("two", _factory_base.SLOTS[1])
    factory.launch_project("first", "alpha")
    factory.launch_project("second", "bravo")
    flyte_world.runs["one"].phase = "SUCCEEDED"
    newest = factory.launch_project("third", "charlie")
    assert (newest["status"], newest["position"]) == ("queued", 2)  # alpha took the slot, bravo is next
    assert [c["inputs"]["name"] for c in flyte_world.created] == ["alpha"]


def test_the_older_request_gets_the_one_free_slot(flyte_world):
    for name, at in (("newer", 2.0), ("older", 1.0)):
        _queue.enqueue(flyte_world.queue, _queue.Request(name, 1, "new", "b", "opus", "r", "", {}, at))
    flyte_world.add("one", _factory_base.SLOTS[0])
    assert projects(flyte_world, factory.dispatch()) == ["older"]


def test_dispatching_twice_does_not_start_a_second_run(flyte_world):
    factory.launch_project("brief", "alpha")
    assert factory.dispatch() == []
    assert len(flyte_world.created) == 1


def test_a_cancelled_request_is_skipped_and_a_started_one_cannot_be_cancelled(flyte_world):
    flyte_world.add("one", _factory_base.SLOTS[0])
    flyte_world.add("two", _factory_base.SLOTS[1])
    factory.launch_project("brief", "alpha")
    factory.launch_project("brief", "bravo")
    factory.cancel("alpha")
    flyte_world.runs["one"].phase = "SUCCEEDED"
    assert projects(flyte_world, factory.dispatch()) == ["bravo"]
    assert "alpha/0001.started" not in written(flyte_world.queue)


def test_a_cancel_aborts_the_live_run_of_a_request_that_was_not_yet_marked_started(flyte_world):
    # a dispatcher created the run and has not written `.started`: cancel must leave no live run
    _queue.enqueue(flyte_world.queue, _queue.Request("late", 1, "new", "b", "opus", "r", "", {}, 5.0))
    run = add_labelled(flyte_world, "late-run", "late", _factory_base.SLOTS[0])
    assert factory.cancel("late") == {"cancelled": [1]}
    assert run.phase == "ABORTED"
    assert _queue.entries(flyte_world.queue, "late")[0].closed == "cancelled"
    assert _queue.marker(flyte_world.queue, "late", 1, "cancelled")["aborted_run"] == "late-run"


def test_a_launch_that_loses_the_slot_race_is_aborted_and_retried_under_a_new_run_name(
    flyte_world, monkeypatch
):
    older = flyte_world.add("older", _factory_base.SLOTS[0])
    looks = []

    def operated_live():
        # first look (dispatch): nothing live; second (after our run exists): the older run beats ours
        looks.append(1)
        return [] if len(looks) == 1 else [flyte_world.runs[flyte_world.created[0]["name"]], older]

    real = factory.operated_live
    monkeypatch.setattr(factory, "operated_live", operated_live)
    result = factory.launch_project("brief", "alpha")
    first = flyte_world.runs[flyte_world.created[0]["name"]]
    assert (first.phase, first.abort_reason) == ("ABORTED", _factory_base.CONTENTION_REASON)
    assert result["status"] == "queued"
    assert "alpha/0001.attempt-1" in written(flyte_world.queue)
    # the abort is recorded in the marker, which is what the next pass reads
    assert _queue.marker(flyte_world.queue, "alpha", 1, "attempt-1")["aborted_run"] == first.name
    monkeypatch.setattr(factory, "operated_live", real)  # older holds slot 0, slot 1 is free
    [second] = factory.dispatch()
    assert re.fullmatch(r"alpha-1-1-[0-9a-z]{6}", second)
    assert second != first.name


def test_the_sweep_retries_an_abort_the_marker_records_and_closes_any_other_abort(flyte_world):
    for name in ("alpha", "bravo"):
        _queue.enqueue(flyte_world.queue, _queue.Request(name, 1, "new", "b", "opus", "r", "", {}, 1.0))
    flyte_world.add("one", _factory_base.SLOTS[0])
    flyte_world.add("two", _factory_base.SLOTS[1])
    add_labelled(flyte_world, "alpha-run", "alpha", _factory_base.SLOTS[0], phase="ABORTED")
    _queue.mark(flyte_world.queue, "alpha", 1, "attempt-1", aborted_run="alpha-run")
    add_labelled(
        flyte_world, "bravo-run", "bravo", _factory_base.SLOTS[0], phase="ABORTED"
    )  # a human's abort
    assert factory.dispatch() == []
    assert [(e.name, e.attempt) for e in _queue.pending(flyte_world.queue)] == [("alpha", 1)]
    failed = _queue.marker(flyte_world.queue, "bravo", 1, "failed")
    assert "bravo-run was aborted" in failed["error"]


def test_an_invalid_input_closes_the_request_and_a_system_error_leaves_it_pending(flyte_world):
    for name, at in (("alpha", 1.0), ("bravo", 2.0)):
        _queue.enqueue(flyte_world.queue, _queue.Request(name, 1, "new", "b", "opus", "r", "", {}, at))
    flyte_world.fail_create = [flyte.errors.RuntimeUserError("InvalidArgumentError", "bad input")]
    assert projects(flyte_world, factory.dispatch()) == ["bravo"]
    assert "bad input" in _queue.marker(flyte_world.queue, "alpha", 1, "failed")["error"]
    run_of(flyte_world, "bravo").phase = "SUCCEEDED"
    _queue.enqueue(flyte_world.queue, _queue.Request("charlie", 1, "new", "b", "opus", "r", "", {}, 3.0))
    flyte_world.fail_create = [flyte.errors.RuntimeSystemError("SystemUnavailableError", "down")]
    assert factory.dispatch() == []
    assert [e.name for e in _queue.pending(flyte_world.queue)] == ["charlie"]
    assert projects(flyte_world, factory.dispatch()) == ["charlie"]


def test_the_ending_run_is_neither_busy_nor_a_contender(flyte_world):
    flyte_world.add("ending", _factory_base.SLOTS[0])
    flyte_world.add("other", _factory_base.SLOTS[1])
    _queue.enqueue(flyte_world.queue, _queue.Request("alpha", 1, "new", "b", "opus", "r", "", {}, 1.0))
    assert factory.dispatch() == []
    assert projects(flyte_world, factory.dispatch(exclude_run="ending")) == ["alpha"]
    assert {r.phase for r in flyte_world.runs.values()} == {"RUNNING"}
    assert run_of(flyte_world, "alpha").task == _factory_base.SLOTS[0]


def test_a_finished_project_resumes_from_its_workspace_with_the_new_text_as_direction(flyte_world):
    factory.launch_project("build the thing", "proj")
    first = run_of(flyte_world, "proj")
    first.phase, first.outputs_ = "SUCCEEDED", ("proj: done", Dir(path="s3://bucket/ws"))
    result = factory.launch_project("now add tests", "proj")
    assert (result["status"], result["kind"], result["gen"]) == ("started", "resume", 2)
    second = flyte_world.created[1]
    assert second["inputs"]["workspace_in"].path == "s3://bucket/ws"
    assert second["inputs"]["brief"].startswith("build the thing\n\n## Further direction (")
    assert second["inputs"]["brief"].endswith("\n\nnow add tests")
    assert re.fullmatch(r"proj-2-0-[0-9a-z]{6}", second["name"])
    assert second["context"]["labels"] == {"factory-project": "proj", "factory-gen": "2"}


def test_a_resume_with_no_workspace_starts_fresh_and_says_so(flyte_world):
    factory.launch_project("build the thing", "proj")
    run_of(flyte_world, "proj").phase = "FAILED"
    factory.launch_project("again", "proj")
    inputs = flyte_world.created[1]["inputs"]
    assert "workspace_in" not in inputs
    assert "starts from an empty workspace" in inputs["brief"]


def test_a_name_from_before_the_queue_is_a_new_project(flyte_world):
    flyte_world.add("legacy-run", _factory_base.SLOTS[0], phase="SUCCEEDED", labels={})
    assert factory.launch_project("brief", "legacy-run")["kind"] == "new"


def test_an_empty_queue_costs_no_flyte_listing(flyte_world):
    assert factory.dispatch() == []
    assert flyte_world.listings == 0


# --- races: duplicate runs and cancel -----------------------------------------------------------------


def live_of(world, project):
    return [
        r
        for r in world.runs.values()
        if r.pb2.labels.get(_factory_base.LABEL_PROJECT) == project and r.phase == "RUNNING"
    ]


@pytest.fixture
def no_lease(monkeypatch):
    """The lease is approximate, so a race can get past it: take it away to test what catches those."""
    monkeypatch.setattr(_queue, "take_lease", lambda *a, **kw: None)


def test_two_dispatchers_racing_on_one_request_leave_exactly_one_live_run(flyte_world, no_lease):
    _queue.enqueue(flyte_world.queue, _queue.Request("alpha", 1, "new", "b", "opus", "r", "", {}, 1.0))
    results = []
    # B dispatches the same request while A is between its marker check and its create: B's run is older
    flyte_world.before_create = lambda: results.append(factory.dispatch())
    mine = factory.dispatch()
    [theirs] = results[0]
    assert mine == []  # A lost: its run was aborted, not reported as started
    [live] = live_of(flyte_world, "alpha")
    assert live.name == theirs
    assert len(flyte_world.created) == 2
    assert _queue.marker(flyte_world.queue, "alpha", 1, "started")["run"] == theirs
    assert [e.closed for e in _queue.entries(flyte_world.queue)] == ["started"]


def test_the_oldest_duplicate_keeps_the_request_and_aborts_the_others(flyte_world, no_lease):
    _queue.enqueue(flyte_world.queue, _queue.Request("alpha", 1, "new", "b", "opus", "r", "", {}, 1.0))
    # a newer duplicate appears between our create and our dedupe
    flyte_world.after_create = lambda: add_labelled(flyte_world, "zzz-dup", "alpha", _factory_base.SLOTS[1])
    [mine] = factory.dispatch()
    assert [r.name for r in live_of(flyte_world, "alpha")] == [mine]
    assert flyte_world.runs["zzz-dup"].phase == "ABORTED"
    assert _queue.marker(flyte_world.queue, "alpha", 1, "started")["run"] == mine


def test_duplicates_are_ordered_by_creation_even_when_start_times_tie(flyte_world, no_lease):
    _queue.enqueue(flyte_world.queue, _queue.Request("alpha", 1, "new", "b", "opus", "r", "", {}, 1.0))
    flyte_world.after_create = lambda: add_labelled(flyte_world, "aaa-dup", "alpha", _factory_base.SLOTS[1])
    orig_add = flyte_world.add

    def add_same_time(name, task, **kw):
        run = orig_add(name, task, **kw)
        run.action.start_time = flyte_world.runs[flyte_world.created[0]["name"]].action.start_time
        return run

    flyte_world.add = add_same_time
    [mine] = factory.dispatch()  # created first, so it keeps the request whatever the names or start times
    assert [r.name for r in live_of(flyte_world, "alpha")] == [mine]
    assert flyte_world.runs["aaa-dup"].phase == "ABORTED"


def test_the_dedupe_outcome_is_not_read_from_abort_reasons(flyte_world, no_lease):
    assert not hasattr(factory, "_abort_reason")
    _queue.enqueue(flyte_world.queue, _queue.Request("alpha", 1, "new", "b", "opus", "r", "", {}, 1.0))
    flyte_world.before_create = factory.dispatch
    factory.dispatch()
    aborted = [r for r in flyte_world.runs.values() if r.phase == "ABORTED"]
    assert len(aborted) == 1
    assert aborted[0].details().action_details.abort_info.reason is None


def test_cancel_between_the_marker_check_and_the_create_leaves_no_live_run(flyte_world):
    # dispatch checks the marker -> cancel writes it -> cancel lists no run -> dispatch creates
    _queue.enqueue(flyte_world.queue, _queue.Request("alpha", 1, "new", "b", "opus", "r", "", {}, 1.0))
    flyte_world.before_create = lambda: factory.cancel("alpha")
    assert factory.dispatch() == []
    assert live_of(flyte_world, "alpha") == []
    [run] = flyte_world.created
    assert flyte_world.runs[run["name"]].phase == "ABORTED"
    entry = _queue.entries(flyte_world.queue)[0]
    assert entry.closed == "cancelled"
    assert _queue.marker(flyte_world.queue, "alpha", 1, "cancelled")["aborted_run"] == run["name"]


def test_cancel_after_the_create_aborts_the_run_it_finds(flyte_world):
    _queue.enqueue(flyte_world.queue, _queue.Request("alpha", 1, "new", "b", "opus", "r", "", {}, 1.0))
    flyte_world.after_create = lambda: factory.cancel("alpha")
    assert factory.dispatch() == []
    assert live_of(flyte_world, "alpha") == []
    assert _queue.entries(flyte_world.queue)[0].closed == "cancelled"


def test_the_fake_replaces_a_run_created_under_an_existing_name(flyte_world):
    # measured on the tenant: no RunAlreadyExistsError, the second call's inputs win
    flyte_world.add("same", _factory_base.SLOTS[0], inputs={"n": 1})
    run = flyte.with_runcontext(name="same").run("task", n=2)
    assert flyte_world.runs["same"] is run and run.inputs() == {"n": 2}
    assert len(flyte_world.runs) == 1


# --- QA round 1: the resume source ----------------------------------------------------------------------


def finished_gen(world, name, gen, phase, outputs=None, slot=0):
    """Generation `gen` of `name` as the queue leaves it: request, started marker and a run in `phase`."""
    _queue.enqueue(world.queue, _queue.Request(name, gen, "new", "build it", "opus", "r", "", {}, float(gen)))
    _queue.mark(world.queue, name, gen, "started", run=f"{name}-g{gen}")
    return add_labelled(
        world, f"{name}-g{gen}", name, _factory_base.SLOTS[slot], gen=gen, phase=phase, outputs=outputs
    )


def test_a_failed_newest_run_does_not_lose_the_older_good_workspace(flyte_world):
    finished_gen(flyte_world, "proj", 1, "SUCCEEDED", ("done", Dir(path="s3://bucket/gen1")))
    finished_gen(flyte_world, "proj", 2, "FAILED")
    factory.launch_project("again", "proj")
    inputs = flyte_world.created[0]["inputs"]
    assert inputs["workspace_in"].path == "s3://bucket/gen1"
    assert "starts from an empty workspace" not in inputs["brief"]


def test_a_succeeded_run_whose_outputs_cannot_be_read_falls_back_to_the_next_older(flyte_world):
    finished_gen(flyte_world, "proj", 1, "SUCCEEDED", ("done", Dir(path="s3://bucket/gen1")))
    finished_gen(flyte_world, "proj", 2, "SUCCEEDED", outputs=None)  # outputs() raises
    finished_gen(flyte_world, "proj", 3, "SUCCEEDED", ("done", "no workspace published"))
    factory.launch_project("again", "proj")
    assert flyte_world.created[0]["inputs"]["workspace_in"].path == "s3://bucket/gen1"


@pytest.mark.parametrize("phase", ["ABORTED", "FAILED"])
def test_an_aborted_duplicate_or_cancelled_run_is_never_the_workspace_source(flyte_world, phase):
    finished_gen(flyte_world, "proj", 1, "SUCCEEDED", ("done", Dir(path="s3://bucket/gen1")))
    # even one that somehow has outputs: only a run that SUCCEEDED is a source
    finished_gen(flyte_world, "proj", 2, phase, ("x", Dir(path="s3://bucket/not-this")))
    factory.launch_project("again", "proj")
    assert flyte_world.created[0]["inputs"]["workspace_in"].path == "s3://bucket/gen1"


# --- QA round 1: a cancel landing after the dispatcher's own checks ------------------------------------


def cancel_at(monkeypatch, where):
    """Run `factory.cancel("alpha")` just after the dispatch step `where` ("keep_oldest", "before_started",
    "started")."""
    real_keep, real_started = _factory_recreate._keep_oldest, factory._started

    def keep_oldest(entry, run):
        keeper = real_keep(entry, run)
        if where == "keep_oldest":
            factory.cancel("alpha")
        return keeper

    def started(queue, *args, **kwargs):
        stale = _queue.pending(queue, "alpha")  # what a cancel that began before `.started` was written saw
        if where == "before_started":
            factory.cancel("alpha")
        real_started(queue, *args, **kwargs)
        if where == "started":
            monkeypatch.setattr(_queue, "pending", lambda *a, **kw: stale)
            factory.cancel("alpha")

    monkeypatch.setattr(_factory_recreate, "_keep_oldest", keep_oldest)
    monkeypatch.setattr(factory, "_started", started)


@pytest.mark.parametrize("where", ["keep_oldest", "before_started", "started"])
def test_a_cancel_after_the_second_check_leaves_no_live_run_and_reads_as_cancelled(
    flyte_world, monkeypatch, where
):
    _queue.enqueue(flyte_world.queue, _queue.Request("alpha", 1, "new", "b", "opus", "r", "", {}, 1.0))
    cancel_at(monkeypatch, where)
    factory.dispatch()
    assert live_of(flyte_world, "alpha") == []
    [run] = flyte_world.created
    assert flyte_world.runs[run["name"]].phase == "ABORTED"
    assert [e.closed for e in _queue.entries(flyte_world.queue)] == ["cancelled"]
    [recent] = factory.queue_view()["recent"]
    assert recent["outcome"] == "cancelled"


@pytest.mark.parametrize("where", ["keep_oldest", "before_started", "started"])
def test_launch_project_does_not_report_started_for_a_run_a_cancel_aborted(flyte_world, monkeypatch, where):
    cancel_at(monkeypatch, where)
    # `cancel("alpha")` needs a pending request: the first call is made from inside dispatch, which has one
    with pytest.raises(ValueError, match="did not start: cancelled"):
        factory.launch_project("brief", "alpha")
    assert live_of(flyte_world, "alpha") == []


# --- QA round 1: contention aborts only the launcher's own run ------------------------------------------


def test_a_third_dispatcher_with_a_stale_listing_aborts_only_its_own_run(flyte_world):
    # E is a project handing over in its last act, N its successor: both are live on slot 0 for a moment.
    # A dispatcher that does not exclude E (it is not E's own run) sees both, and must yield, not abort them.
    ending = flyte_world.add("ending", _factory_base.SLOTS[0])
    successor = flyte_world.add("successor", _factory_base.SLOTS[0])
    flyte_world.add("other", _factory_base.SLOTS[1])
    with pytest.raises(factory.SlotContention):
        factory.launch_on_slot(_factory_base.SLOTS[0], "brief", "xproj", run_name="x-1-0-aaaaaa")
    assert (ending.phase, successor.phase) == ("RUNNING", "RUNNING")
    assert flyte_world.runs["x-1-0-aaaaaa"].phase == "ABORTED"
    assert flyte_world.runs["other"].phase == "RUNNING"


def test_two_newcomers_may_both_yield_but_no_other_run_is_ever_aborted(flyte_world):
    holder = flyte_world.add("holder", _factory_base.SLOTS[0])
    flyte_world.add("other", _factory_base.SLOTS[1])
    seen = []

    def second_newcomer():  # appears between the first one's create and its listing
        try:
            factory.launch_on_slot(_factory_base.SLOTS[0], "brief", "bproj", run_name="b-1-0-bbbbbb")
        except factory.SlotContention as exc:
            seen.append(exc.run_name)

    flyte_world.after_create = second_newcomer
    with pytest.raises(factory.SlotContention):
        factory.launch_on_slot(_factory_base.SLOTS[0], "brief", "aproj", run_name="a-1-0-aaaaaa")
    assert seen == ["b-1-0-bbbbbb"]
    aborted = {r.name for r in flyte_world.runs.values() if r.phase == "ABORTED"}
    assert aborted <= {"a-1-0-aaaaaa", "b-1-0-bbbbbb"}  # the sweep retries whoever yielded
    assert holder.phase == "RUNNING"


def test_the_excluded_run_is_not_a_contender(flyte_world):
    flyte_world.add("ending", _factory_base.SLOTS[0])
    run = factory.launch_on_slot(
        _factory_base.SLOTS[0], "brief", "xproj", exclude_run="ending", run_name="x-1-0-aaaaaa"
    )
    assert run.phase == "RUNNING" and flyte_world.runs["ending"].phase == "RUNNING"


# --- QA round 1: a request that cannot be prepared never wedges the queue ----------------------------------


@pytest.mark.parametrize("stage", ["prepare", "create"])
@pytest.mark.parametrize("error", [ValueError("bad value"), TypeError("bad type"), KeyError("missing")])
def test_a_request_that_fails_to_prepare_or_create_is_closed_failed_and_the_next_one_runs(
    flyte_world, monkeypatch, stage, error
):
    for name, at in (("alpha", 1.0), ("bravo", 2.0)):
        _queue.enqueue(flyte_world.queue, _queue.Request(name, 1, "new", "b", "opus", "r", "", {}, at))
    real_workspace, real_launch = _factory_recreate._workspace_for, factory.launch_on_slot

    def workspace_for(request):
        if request.name == "alpha" and stage == "prepare":
            raise error
        return real_workspace(request)

    def launch_on_slot(slot, brief, name, *args, **kwargs):
        if name == "alpha" and stage == "create":
            raise error
        return real_launch(slot, brief, name, *args, **kwargs)

    monkeypatch.setattr(_factory_recreate, "_workspace_for", workspace_for)
    monkeypatch.setattr(factory, "launch_on_slot", launch_on_slot)
    assert projects(flyte_world, factory.dispatch()) == ["bravo"]
    assert str(error) in _queue.marker(flyte_world.queue, "alpha", 1, "failed")["error"]
    assert [e.closed for e in _queue.entries(flyte_world.queue)] == ["failed", "started"]


# --- QA round 1: names, models and policies ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["ab-", "my-app-", "my--app", "a--", "--ab"])
def test_a_name_that_is_not_a_kubernetes_label_value_is_refused(name):
    with pytest.raises(ValueError, match="name must"):
        _factory_policy.validate(name, "opus")


def test_a_name_with_single_inner_hyphens_is_fine():
    _factory_policy.validate("my-app-2", "opus")
    _factory_policy.validate("a" * 41, "opus")


@pytest.mark.parametrize("model", ["opusé", "٣opus", "opus٠", "op us"])
def test_model_names_are_ascii_only(model):
    with pytest.raises(ValueError, match="model must be"):
        _factory_policy.validate("valid-name", model)


def test_a_cut_that_lands_on_a_hyphen_drops_it():
    name = "a" * 15 + "-" + "b" * 20  # the 30-character budget cuts right after the hyphen
    rid = _queue.run_id(name, 1234, 7)
    assert "--" not in rid and re.fullmatch(r"a+-1234-7-[0-9a-z]{6}", rid)


def test_a_bad_demo_names_the_demo_package(monkeypatch):
    with pytest.raises(ValueError, match="demo_package must be"):
        factory.launch_project("brief", "valid-name", demo="../demo")


@pytest.mark.parametrize(
    "policy", ["{not json", "[1, 2]", '"text"', "3", '{"JUDGE_MODEL": 3}', '{"JUDGE_MODEL": null}']
)
def test_a_malformed_policy_is_a_clear_value_error(flyte_world, policy):
    import asyncio

    with pytest.raises(ValueError, match="policy must be a JSON object of KEY: value strings"):
        asyncio.run(factory.launch.func("brief", "valid-name", policy=policy))
    assert written(flyte_world.queue) == []


def test_normalize_policy_refuses_non_string_values():
    with pytest.raises(ValueError, match="policy must be a JSON object of KEY: value strings"):
        _factory_policy.normalize_policy({"JUDGE_MODEL": 3})  # type: ignore[dict-item]  # deliberately invalid


def test_normalize_policy_refuses_an_unknown_author_engine():
    with pytest.raises(ValueError, match="AUTHOR_ENGINE"):
        _factory_policy.normalize_policy({"AUTHOR_ENGINE": "gpt"})
    assert _factory_policy.normalize_policy({"AUTHOR_ENGINE": " Codex "}) == {
        "AGENT_FACTORY_COORDINATOR_ENGINE": "codex"
    }


@pytest.mark.parametrize("key", ["CLAUDE_JUDGE_ENGINE", "AGENT_FACTORY_CLAUDE_X", "CLAUDE_X"])
def test_normalize_policy_refuses_a_key_with_the_removed_claude_prefix(key):
    with pytest.raises(ValueError, match="must not start with CLAUDE_"):
        _factory_policy.normalize_policy({key: "x"})


# --- QA round 1: everything launch_project can reject is checked before the first write ------------------


def garbage_request(world, name="alpha", gen=1):
    path = Path(world.queue.removeprefix("file://")) / name / f"{gen:04d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    return path


def test_an_unreadable_request_is_not_closed_when_the_new_launch_is_refused(flyte_world):
    garbage_request(flyte_world)
    before = written(flyte_world.queue)
    with pytest.raises(ValueError, match="not allowed"):
        factory.launch_project("brief", "alpha", repo="git@example.org:evil/repo.git")
    assert written(flyte_world.queue) == before == ["alpha/0001.json"]


def test_a_running_name_is_refused_before_an_unreadable_request_is_closed(flyte_world):
    garbage_request(flyte_world)
    flyte_world.add("p1-alpha-abcd", _factory_base.SLOTS[0], labels={_factory_base.LABEL_PROJECT: "alpha"})
    with pytest.raises(ValueError, match="direct-"):
        factory.launch_project("brief", "alpha")
    assert written(flyte_world.queue) == ["alpha/0001.json"]


def test_a_combined_brief_that_is_too_long_is_refused_before_an_unreadable_request_is_closed(
    flyte_world, monkeypatch
):
    monkeypatch.setattr(factory, "MAX_BRIEF", 100)
    finished_gen(flyte_world, "alpha", 1, "SUCCEEDED", ("done", Dir(path="s3://bucket/gen1")))
    garbage_request(flyte_world, gen=2)
    before = written(flyte_world.queue)
    with pytest.raises(ValueError, match="brief plus the new direction"):
        factory.launch_project("b" * 95, "alpha")
    assert written(flyte_world.queue) == before
    assert flyte_world.created == []


def test_an_unreadable_request_is_closed_once_the_launch_is_accepted(flyte_world):
    garbage_request(flyte_world)
    result = factory.launch_project("brief", "alpha")
    assert result["gen"] == 2 and result["status"] == "started"
    assert "unreadable" in _queue.marker(flyte_world.queue, "alpha", 1, "failed")["error"]


def test_login_meters_deduplicates_shared_login_and_takes_newest(monkeypatch):
    now = 1_800_000_000
    first = _meter.Meter("codex shared", "probe", now - 30, 10, now + 100, 20, now + 1000)
    newer = _meter.Meter("codex shared", "probe", now, 30, now + 100, 40, now + 1000)
    monkeypatch.setattr(_factory_meters, "slot_meters", lambda slots: {"one": [first], "two": [newer]})
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: [])
    assert _factory_meters.login_meters(["one", "two"], now=now)["codex shared"].weekly_pct == 40


def test_idle_probe_output_retains_current_refusal_after_auth_failure(monkeypatch):
    now = 1_800_000_000
    limited = _meter.Meter("codex shared", "probe", now - 600, limited_until=now + 900)
    failed = _meter.Meter("codex shared", "refresh-failed", now, error="auth-unavailable")
    # The newest-first control-plane listing is intentional: the older refusal still applies.
    runs = [SimpleNamespace(outputs=lambda m=m: (json.dumps(m.to_dict()),)) for m in (failed, limited)]
    monkeypatch.setattr(_factory_meters, "slot_meters", lambda slots: {})
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: runs)
    meter = _factory_meters.login_meters([], now=now)["codex shared"]
    assert meter.error == "auth-unavailable"
    assert meter.five_hour_pct is None and meter.weekly_pct is None
    assert meter.limited_until == now + 900


# --- a project run aborted from inside, by a session that mistook a0 for an orphan ------------


@pytest.fixture
def checkpoint(flyte_world, monkeypatch):
    """Stand-in for fetching an aborted run's checkpoint: records which location was asked for."""
    asked = []

    def load(location):
        asked.append(location)
        return f"workspace-of[{location}]"

    monkeypatch.setattr(_factory_recreate, "_load_checkpoint_workspace", load)
    return asked


#: The client id of the API key the project's pods authenticate with; recorded by the run at its start.
OWN_KEY = "pods-key-id"


def _sweep() -> list[str]:
    _factory_recreate.recreate_aborted()
    return list(factory.dispatch())


def _aborted_project(
    world: Any, aborted_by: str, run: str = "alpha-run", location: str = "", **kw: Any
) -> Any:
    """Start a project through the queue, with its key and checkpoint noted, and abort its run."""
    _queue.enqueue(world.queue, _queue.Request("alpha", 1, "new", "b", "opus", "r", "", {}, 1.0))
    _queue.mark(world.queue, "alpha", 1, "started", run=run)
    _queue.note_checkpoint(_factory_base.PROFILE.state.uri, run, location, app=OWN_KEY)
    world.add("other", _factory_base.SLOTS[1])
    return add_labelled(
        world, run, "alpha", _factory_base.SLOTS[0], phase="ABORTED", aborted_by=aborted_by, **kw
    )


def _abort_from_inside(world: Any, run_name: str) -> None:
    """Abort the run's root action with the pod's key, as a session in that run would."""
    run = world.runs[run_name]
    run.abort("orphan cleanup")
    run.aborted_by = OWN_KEY
    _queue.note_checkpoint(_factory_base.PROFILE.state.uri, run_name, "", app=OWN_KEY)


def test_a_run_aborted_from_inside_is_recreated_once_from_its_checkpoint(flyte_world, checkpoint):
    """The pod's own key aborting its run is recreated once, from the checkpoint it left."""
    _aborted_project(flyte_world, OWN_KEY, location="s3://b/alpha-run/checkpoint")
    [started] = _sweep()
    assert re.fullmatch(r"alpha-1-1-[0-9a-z]{6}", started)
    assert checkpoint == ["s3://b/alpha-run/checkpoint"]
    [created] = flyte_world.created
    assert created["inputs"]["workspace_in"] == "workspace-of[s3://b/alpha-run/checkpoint]"
    assert "aborted from inside" in created["inputs"]["brief"], "the project is told, in its brief"
    assert _queue.marker(flyte_world.queue, "alpha", 1, "attempt-1")["recreated_from"] == "alpha-run"
    assert _queue.marker(flyte_world.queue, "alpha", 1, "started")["run"] == started


def test_who_aborted_is_read_from_the_root_action_not_the_run(flyte_world, checkpoint):
    """GetRunDetails never carries `abort_info`; reading it there made every abort look like nobody's."""
    run = _aborted_project(flyte_world, OWN_KEY)
    assert getattr(run.details().action_details.abort_info, "aborted_by", None) is None
    assert len(_sweep()) == 1


def test_a_recreated_run_without_a_readable_checkpoint_still_starts_and_says_so(flyte_world, checkpoint):
    """A recreated run with no readable checkpoint still starts, and its brief says so."""
    _aborted_project(flyte_world, OWN_KEY)  # the run noted its key but no checkpoint location
    [started] = _sweep()
    [created] = flyte_world.created
    assert "workspace_in" not in created["inputs"]
    assert "no checkpoint" in created["inputs"]["brief"]
    assert started


def test_an_abort_from_a_person_is_final(flyte_world, checkpoint):
    """An abort by a person is final: the sweep recreates nothing."""
    _aborted_project(flyte_world, "user")
    assert _sweep() == []
    assert flyte_world.created == []


def test_an_abort_nobody_is_named_for_is_final(flyte_world, checkpoint):
    """If the server does not say who aborted, the owner's abort must still be respected."""
    _aborted_project(flyte_world, "")
    assert _sweep() == []
    assert flyte_world.created == []


def test_an_abort_with_another_api_key_is_final(flyte_world, checkpoint):
    """The owner's own scripts abort with an API key too: an application is not by itself the run's key."""
    _aborted_project(flyte_world, "owners-laptop-key")
    assert _sweep() == []
    assert flyte_world.created == []


def test_an_abort_is_final_when_the_run_never_noted_its_key(flyte_world, checkpoint):
    """Without the run's own client id there is nothing to compare, and the owner's abort must stand."""
    _aborted_project(flyte_world, OWN_KEY)
    _queue.note_checkpoint(_factory_base.PROFILE.state.uri, "alpha-run", "s3://b/cp")  # an older run: no key
    assert _sweep() == []
    assert flyte_world.created == []


def test_an_abort_the_factory_recorded_is_not_a_recreate(flyte_world, checkpoint):
    """An abort the factory itself recorded is not a recreate."""
    _aborted_project(flyte_world, OWN_KEY)
    _queue.mark(flyte_world.queue, "alpha", 1, "cancelled", aborted_run="alpha-run")
    assert _sweep() == []
    assert flyte_world.created == []


def test_an_aborted_run_the_queue_did_not_start_is_left_alone(flyte_world, checkpoint):
    """Only the run the queue's `started` marker names is a project run to bring back."""
    _aborted_project(flyte_world, OWN_KEY, run="old-run")
    add_labelled(
        flyte_world, "older-run", "alpha", _factory_base.SLOTS[0], phase="ABORTED", aborted_by=OWN_KEY
    )
    _queue.note_checkpoint(_factory_base.PROFILE.state.uri, "older-run", "", app=OWN_KEY)
    _queue.mark(flyte_world.queue, "alpha", 1, "started", run="old-run", at=0)
    flyte_world.runs["old-run"].aborted_by = "user"
    _sweep()
    assert flyte_world.created == []


def test_a_recreated_run_aborted_again_is_not_recreated_again(flyte_world, checkpoint):
    """A recreated run that is aborted from inside again is not recreated a second time."""
    _aborted_project(flyte_world, OWN_KEY)
    [second] = _sweep()
    _abort_from_inside(flyte_world, second)
    assert _sweep() == []
    assert len(flyte_world.created) == 1, "never a loop"


def test_a_second_internal_abort_of_the_same_request_is_never_recreated(flyte_world, checkpoint, monkeypatch):
    """Replaces "an hour later the next internal abort may be recreated", on purpose.

    The cap was once per project per hour, so a session that kept killing its own run restarted it every
    hour forever. It is now once per (project, gen) ever: a new gen comes only from the owner.
    """
    _aborted_project(flyte_world, OWN_KEY)
    [second] = _sweep()
    _abort_from_inside(flyte_world, second)
    later = time.time() + 30 * 24 * 3600
    monkeypatch.setattr(time, "time", lambda: later)
    assert _sweep() == []
    assert len(flyte_world.created) == 1


def test_no_recreate_while_a_run_of_the_request_is_live(flyte_world, checkpoint):
    """A dispatcher that raced the abort may already have started one; a second would be a duplicate."""
    _aborted_project(flyte_world, OWN_KEY)
    add_labelled(flyte_world, "alpha-racer", "alpha", _factory_base.SLOTS[0], phase="RUNNING")
    _factory_recreate.recreate_aborted()
    assert _queue.marker(flyte_world.queue, "alpha", 1, "started")["run"] == "alpha-run"
    assert not _queue.marker(flyte_world.queue, "alpha", 1, "attempt-1")


def test_the_listing_looks_back_past_a_sweep_that_queued(flyte_world, monkeypatch):
    """An abort just after one sweep listed must still be in the next sweep's listing, even a late one."""
    asked = []

    def listall(**kw: Any) -> list[Any]:
        asked.append(kw["updated_at"].after)
        return []

    monkeypatch.setattr(remote.Run, "listall", listall)
    monkeypatch.setattr(_deploy, "QUEUE_SWEEP_MINUTES", 60)
    _factory_recreate.recreate_aborted()
    done = datetime.now(timezone.utc)
    assert asked, "the aborted runs are listed"
    assert all(after <= done - timedelta(minutes=2 * 60 + 5) for after in asked)


def test_a_failing_recreate_does_not_stop_the_sweep_dispatching(flyte_world, monkeypatch):
    """A recreate whose listing fails does not stop the same sweep dispatching the queue."""
    import asyncio

    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    listall = remote.Run.listall

    def flaky(**kw):
        if kw.get("in_phase") == ("ABORTED",):
            raise ConnectError(Code.UNAVAILABLE, "listing down")
        return listall(**kw)

    monkeypatch.setattr(remote.Run, "listall", flaky)
    _queue.enqueue(flyte_world.queue, _queue.Request("beta", 1, "new", "b", "opus", "r", "", {}, 1.0))
    result = json.loads(asyncio.run(factory.sweep.func()))
    assert len(result["dispatched"]) == 1


def test_the_workspace_is_read_out_of_a_saved_checkpoint(tmp_path, monkeypatch):
    """The checkpoint is a tar of the state root (`workspace/` beside `claude/`); only `workspace` resumes."""
    import flyte

    root = tmp_path / "root"
    (root / "workspace").mkdir(parents=True)
    (root / "workspace" / "work.txt").write_text("done so far")
    (root / "claude").mkdir()
    location = f"file://{tmp_path / 'cp' / 'checkpoint'}"
    flyte.Checkpoint(checkpoint_dest=location).save_sync(root)
    # The real one uploads the directory before the checkpoint's temporary copy goes away.
    monkeypatch.setattr(flyte.io.Dir, "from_local", lambda path: (Path(path) / "work.txt").read_text())

    assert _factory_recreate._load_checkpoint_workspace(location) == "done so far"  # type: ignore[comparison-overlap]  # the fake from_local returns text
    assert _factory_recreate._load_checkpoint_workspace(f"file://{tmp_path / 'nowhere'}") is None


@pytest.mark.parametrize("phase", ["ABORTED", "FAILED"])
def test_a_slot_freed_by_an_abnormal_end_is_dispatched_by_the_next_sweep(flyte_world, phase):
    """No hand-over runs for an aborted or crashed holder, so the sweep is what starts the next launch."""
    import asyncio

    for slot in _factory_base.SLOTS:
        flyte_world.add(f"holder-{slot}", slot)
    _queue.enqueue(flyte_world.queue, _queue.Request("waiting", 1, "new", "b", "opus", "r", "", {}, 1.0))
    assert json.loads(asyncio.run(factory.sweep.func()))["dispatched"] == []
    flyte_world.runs[f"holder-{_factory_base.SLOTS[0]}"].phase = phase
    result = json.loads(asyncio.run(factory.sweep.func()))
    assert projects(flyte_world, result["dispatched"]) == ["waiting"]


@pytest.mark.parametrize("phase", ["ABORTED", "FAILED"])
def test_a_slot_freed_by_an_abnormal_end_wakes_the_planner_within_one_sweep(flyte_world, monkeypatch, phase):
    """Planner admission never dispatches in the sweep, and no hand-over wakes it for a crashed holder."""
    import asyncio

    from flyte_agent_factory import _planner_runtime, _profile

    woken = []
    monkeypatch.setattr(
        _factory_base,
        "PROFILE",
        dataclasses.replace(_factory_base.PROFILE, planner=_profile.Planner(admission="planner")),
    )
    monkeypatch.setattr(_planner_runtime, "notify", lambda profile, key, run_name="": woken.append(key))
    monkeypatch.setenv(_deploy.GITHUB_KEY_ENV, "offline-app-key")
    for slot in _factory_base.SLOTS:
        flyte_world.add(f"holder-{slot}", slot)
    _queue.enqueue(flyte_world.queue, _queue.Request("waiting", 1, "new", "b", "opus", "r", "", {}, 1.0))

    assert json.loads(asyncio.run(factory.sweep.func()))["dispatched"] == []
    assert woken == [], "every slot is held: nothing to admit"
    flyte_world.runs[f"holder-{_factory_base.SLOTS[0]}"].phase = phase
    assert json.loads(asyncio.run(factory.sweep.func()))["dispatched"] == []
    assert woken == ["offline-app-key"], "the freed slot did not wake the planner"


def test_the_sweep_does_not_wake_the_planner_when_nothing_waits(flyte_world, monkeypatch):
    """The sweep leaves the planner asleep when no request waits."""
    import asyncio

    from flyte_agent_factory import _planner_runtime, _profile

    woken = []
    monkeypatch.setattr(
        _factory_base,
        "PROFILE",
        dataclasses.replace(_factory_base.PROFILE, planner=_profile.Planner(admission="planner")),
    )
    monkeypatch.setattr(_planner_runtime, "notify", lambda *a, **k: woken.append(a))
    asyncio.run(factory.sweep.func())
    assert woken == []


def test_one_slot_contention_does_not_starve_the_launches_behind_it(flyte_world, monkeypatch):
    """One launch lost to slot contention does not starve the launches queued behind it."""
    for name, at in (("first", 1.0), ("second", 2.0)):
        _queue.enqueue(flyte_world.queue, _queue.Request(name, 1, "new", "b", "opus", "r", "", {}, at))
    real = factory.launch_on_slot
    taken = []

    def contended(slot, brief, name, *args, **kwargs):
        if name == "first":
            raise factory.SlotContention("a rival launch holds it", "rival-run")
        taken.append(slot)
        return real(slot, brief, name, *args, **kwargs)

    monkeypatch.setattr(factory, "launch_on_slot", contended)
    assert projects(flyte_world, factory.dispatch()) == ["second"]
    assert taken and taken[0] != _factory_base.SLOTS[0], (
        "the contended slot is not offered to the next launch"
    )


def test_a_listing_error_in_the_planner_wake_does_not_fail_the_sweep(flyte_world, monkeypatch):
    """A listing error while waking the planner does not fail the sweep."""
    import asyncio

    from flyte_agent_factory import _profile

    monkeypatch.setattr(
        _factory_base,
        "PROFILE",
        dataclasses.replace(_factory_base.PROFILE, planner=_profile.Planner(admission="planner")),
    )
    _queue.enqueue(flyte_world.queue, _queue.Request("waiting", 1, "new", "b", "opus", "r", "", {}, 1.0))

    def flaky():
        raise ConnectionResetError("listing reset")

    # Planner mode dispatches nothing, so the wake's live listing is the one this sweep reads.
    monkeypatch.setattr(factory, "operated_live", flaky)
    assert json.loads(asyncio.run(factory.sweep.func()))["dispatched"] == []


# --- factory.status carries the decisions the factory made in the owner's place ------------------------


def _status_of(monkeypatch, data, rows=40):
    from types import SimpleNamespace

    from flyte_agent_factory import _dashboard, _factory, _factory_base

    run = SimpleNamespace(name="r1", url="https://console.example/r1", pb2=SimpleNamespace(labels={}))
    monkeypatch.setattr(_factory_base, "_labelled_runs", lambda *a, **k: [run])
    monkeypatch.setattr(_factory_base, "_phase", lambda r: next(iter(_factory_base.LIVE)))
    monkeypatch.setattr(_factory_base, "report_html", lambda r: _dashboard.status_script(data))
    return _factory.read_status("proj", rows=rows)


def _decision(n, **extra):
    return {
        "id": f"decision-{n:03d}",
        "at": 1000.0 + n,
        "question": f"Question {n}?",
        "decision": f"Do {n}.",
        "reasoning": f"Because {n}.",
        "approve": None,
        "request": f"r{n}",
        "private": "must not be returned",
        **extra,
    }


def test_status_returns_the_decisions_oldest_first_with_overruled_spelled_out(monkeypatch):
    data = {
        "schema": 1,
        "timeline": [],
        "decisions": [_decision(1), _decision(2, overruled_at=1500.0, overruled_by="Union: no")],
    }
    result = _status_of(monkeypatch, data)
    assert [d["id"] for d in result["decisions"]] == ["decision-001", "decision-002"]
    first, second = result["decisions"]
    assert (
        first["overruled"] is False and second["overruled"] is True and second["overruled_by"] == "Union: no"
    )
    assert (
        first["question"] == "Question 1?"
        and first["decision"] == "Do 1."
        and first["reasoning"] == "Because 1."
    )
    assert "private" not in first and "request" not in first


def test_status_has_paused_field(monkeypatch):
    """A paused run's status names who it waits for, until when and why not the others, with a dated label."""
    from datetime import datetime, timezone

    until = datetime(2030, 1, 7, 0, 40, tzinfo=timezone.utc).timestamp()
    paused = {
        "engine": "claude",
        "until": until,
        "others": {"codex": "pinned to claude (ENGINE_FALLBACK unset)"},
        "needs_human": "",
    }
    phase = {"kind": "limited", "since": 1000.0, "round": 0, "limited_until": until, "paused": paused}
    result = _status_of(monkeypatch, {"schema": 1, "timeline": [], "phase": phase})
    assert result["paused"] == paused
    assert result["phase"] == "limited until Mon 7 Jan 00:40 UTC, waiting for claude"

    running = {"kind": "building", "since": 1000.0, "round": 0, "limited_until": None}
    result = _status_of(monkeypatch, {"schema": 1, "timeline": [], "phase": running})
    assert result["paused"] is None and result["phase"] == "building"


def test_status_has_no_decisions_for_a_report_without_any(monkeypatch):
    assert _status_of(monkeypatch, {"schema": 1, "timeline": []})["decisions"] == []


def test_status_skips_decisions_that_are_not_records(monkeypatch):
    assert _status_of(monkeypatch, {"schema": 1, "timeline": [], "decisions": "oops"})["decisions"] == []
    data = {"schema": 1, "timeline": [], "decisions": ["oops", 3, _decision(1)]}
    assert [d["id"] for d in _status_of(monkeypatch, data)["decisions"]] == ["decision-001"]


def test_status_shrinks_its_timeline_and_then_its_decisions_to_fit_the_size_limit(monkeypatch):
    import json

    big = "x" * 900
    data = {
        "schema": 1,
        "timeline": [{"time": i, "icon": "🔧", "text": big} for i in range(60)],
        "decisions": [_decision(n, reasoning=big, decision=big) for n in range(1, 21)],
    }
    result = _status_of(monkeypatch, data)
    assert len(json.dumps(result)) <= 40_000
    assert "error" not in result and result["decisions"], "decisions stay while there is a timeline to cut"
    # The newest decisions are the ones kept.
    assert result["decisions"][-1]["id"] == "decision-020"
