"""Deploy exclusion and release history use Flyte, never the old storage records."""

import hashlib
import re
from types import SimpleNamespace
from typing import cast

import pytest
from flyte import remote

from flyte_agent_factory import _deployer, _profile
from flyte_agent_factory._github import GithubApp
from tests.test_deployer import NEW_SHA, OLD_SHA, SHA, GitRefs, both_paths
from tests.test_deployer import directed as directed
from tests.test_deployer import world as world

# These tests never reach the app: admission is decided by the stubbed listing or `_acquire_ref`.
_APP = cast("GithubApp", object())


async def test_newest_readable_deployment_skips_stand_downs(world, monkeypatch):
    def run(name, output):
        async def outputs():
            if isinstance(output, Exception):
                raise output
            return (output,)

        return SimpleNamespace(
            name=name, pb2=SimpleNamespace(labels={"deploy-sha": name}), outputs=SimpleNamespace(aio=outputs)
        )

    async def listing(**kwargs):
        assert kwargs["task_name"] == "factory-deployer.deploy_pair"
        assert kwargs["with_labels"] == _deployer.release_labels(world.profile)
        for candidate in [
            run("stand-down", {"deployed": False}),
            run("broken", OSError()),
            run("actual", {"deployed": True, "envs": ["factory"], "sha": "a" * 40, "direction_sha": ""}),
        ]:
            yield candidate

    monkeypatch.setattr(remote.Run, "listall", SimpleNamespace(aio=listing))
    release = await _deployer.deployed_pair(world.profile)
    assert release is not None
    deployed, pair = release
    assert deployed.name == "actual" and pair == ("a" * 40, "")


@pytest.mark.parametrize("consistent", [True, False])
async def test_pair_exclusion_uses_recorded_probe_result(world, consistent, monkeypatch):
    monkeypatch.setattr(_deployer, "RUN_LISTING_CONSISTENT", consistent)
    monkeypatch.setattr(_deployer, "SETTLE", 0)
    calls = []

    async def listing(**kwargs):
        for name in ["older", "younger"]:
            yield SimpleNamespace(name=name)

    def acquire(*args):
        calls.append(args)
        return False

    monkeypatch.setattr(remote.Run, "listall", SimpleNamespace(aio=listing))
    monkeypatch.setattr(_deployer, "_acquire_ref", acquire)
    assert not await _deployer._admitted(world.profile, _APP, "younger", "a" * 40)
    assert bool(calls) is not consistent


async def test_oldest_live_pair_wins_even_across_different_heads(world, monkeypatch):
    monkeypatch.setattr(_deployer, "RUN_LISTING_CONSISTENT", True)
    monkeypatch.setattr(_deployer, "SETTLE", 0)

    async def listing(**kwargs):
        assert kwargs["sort_by"] == ("created_at", "asc")
        assert "deploy-sha" not in kwargs["with_labels"]
        for name in ["older", "younger"]:
            yield SimpleNamespace(name=name)

    monkeypatch.setattr(remote.Run, "listall", SimpleNamespace(aio=listing))
    assert await _deployer._admitted(world.profile, _APP, "older", "a" * 40)
    assert not await _deployer._admitted(world.profile, _APP, "younger", "b" * 40)


@both_paths
async def test_live_pair_stops_tick(world):
    world.live_runs = ["other"]
    assert await world.tick() == "deploy in flight"
    assert not world.deployed
    assert not world.submissions


@both_paths
async def test_a_timeout_counts_without_a_failure_record(world):
    labels = _deployer._pair_labels(world.profile, "a" * 40, "")
    for _ in range(3):
        world.add_run("TIMED_OUT", labels=labels)
    assert "giving up" in await world.tick()
    assert not world.deployed and not world.state.exists()


@both_paths
async def test_attempts_before_the_latest_success_do_not_count(world):
    labels = _deployer._pair_labels(world.profile, "a" * 40, "")
    for _ in range(3):
        world.add_run("ABORTED", labels=labels)
    world.record("b" * 40)
    await world.tick()
    assert world.last["sha"] == "a" * 40


async def test_stale_repository_history_is_not_used(world):
    labels = {**_deployer._pair_labels(world.profile, "a" * 40, ""), "deploy-repo": "old-repo"}
    for _ in range(3):
        world.add_run("FAILED", labels=labels)
    world.add_run("SUCCEEDED", labels=labels, output={"deployed": True, "envs": ["factory"]})
    await world.tick()
    assert world.deployed
    assert all(request.url.path.startswith(("/repos/acme/factory", "/app/")) for request in world.gh.requests)


async def test_a_stale_queued_pair_never_fetches_the_old_commit(world, monkeypatch):
    called = []
    monkeypatch.setattr(_deployer, "_acquire_ref", lambda *args: called.append(args))
    output = await _deployer.run_pair(world.profile, world.app, "old-attempt", "b" * 40, "")
    assert output == {"deployed": False}
    assert not called and not world.fetched
    assert world.submissions[-1][0] == "factory-deployer.deploy_if_changed"


@pytest.mark.parametrize("race", [False, True])
def test_git_ref_create_is_exclusive(world, race):
    app = GitRefs()
    app.race = race
    assert _deployer._acquire_ref(world.profile, cast("GithubApp", app), "first", "a" * 40) is not race


@pytest.mark.parametrize("phase", ["RUNNING", "QUEUED", "FAILED", "TIMED_OUT", "ABORTED", "SUCCEEDED"])
def test_git_ref_can_only_replace_an_authoritatively_terminal_holder(world, monkeypatch, phase):
    app = GitRefs()
    assert _deployer._acquire_ref(world.profile, cast("GithubApp", app), "first", "a" * 40)
    old_ref = app.ref
    monkeypatch.setattr(remote.Run, "get", lambda **kw: SimpleNamespace(phase=phase))
    terminal = phase in _deployer._TERMINAL
    assert _deployer._acquire_ref(world.profile, cast("GithubApp", app), "second", "a" * 40) is terminal
    assert (app.ref != old_ref) is terminal


def test_git_ref_reclaim_race_does_not_admit_both(world, monkeypatch):
    app = GitRefs()
    assert _deployer._acquire_ref(world.profile, cast("GithubApp", app), "first", "a" * 40)
    monkeypatch.setattr(remote.Run, "get", lambda **kw: SimpleNamespace(phase="ABORTED"))
    app.race = True
    assert not _deployer._acquire_ref(world.profile, cast("GithubApp", app), "second", "a" * 40)


def test_unreadable_lock_holder_fails_closed(world, monkeypatch):
    app = GitRefs()
    assert _deployer._acquire_ref(world.profile, cast("GithubApp", app), "first", "a" * 40)

    def missing(**kwargs):
        raise OSError("Flyte unavailable")

    monkeypatch.setattr(remote.Run, "get", missing)
    with pytest.raises(OSError, match="Flyte unavailable"):
        _deployer._acquire_ref(world.profile, cast("GithubApp", app), "second", "a" * 40)


@pytest.mark.parametrize("consistent", [True, False])
async def test_two_pair_attempts_produce_one_deploy(world, monkeypatch, consistent):
    import asyncio

    labels = _deployer._pair_labels(world.profile, SHA, "")
    older = world.add_run("RUNNING", labels=labels)
    younger = world.add_run("RUNNING", labels=labels)
    app = GitRefs()
    monkeypatch.setattr(_deployer, "RUN_LISTING_CONSISTENT", consistent)
    if not consistent:
        real_call = world.app._call

        def call(repo, method, path, **kw):
            if "/git/" in path:
                return app._call(repo, method, path, **kw)
            return real_call(repo, method, path, **kw)

        monkeypatch.setattr(world.app, "_call", call)
        monkeypatch.setattr(remote.Run, "get", lambda **kw: SimpleNamespace(phase="RUNNING"))
    results = await asyncio.gather(
        _deployer.run_pair(world.profile, world.app, older.name, SHA, ""),
        _deployer.run_pair(world.profile, world.app, younger.name, SHA, ""),
    )
    assert sum(result["deployed"] for result in results) == 1
    assert len(world.factory_calls) == 1


@both_paths
async def test_a_younger_pair_placed_after_the_older_one_deployed_stands_down(world):
    # Two ticks each submitted the same pair; the older deployed and finished before the younger
    # was placed, so the younger is admitted, and must find the pair already deployed.
    labels = _deployer._pair_labels(world.profile, SHA, "")
    older = world.add_run("RUNNING", labels=labels, inputs={"sha": SHA, "direction_sha": ""})
    younger = world.add_run("QUEUED", labels=labels, inputs={"sha": SHA, "direction_sha": ""})
    assert (await world.attempt(older)).startswith(f"deployed {SHA}")
    younger.phase = "RUNNING"
    assert await world.attempt(younger) == f"up to date at {SHA}"
    assert younger.output == {"deployed": False}
    assert len(world.factory_calls) == 1


async def test_more_stand_downs_than_a_listing_page_do_not_hide_the_release(world):
    world.record(SHA)
    stand_down = _deployer._pair_labels(world.profile, SHA, "")
    for _ in range(150):  # more than the SDK's default page of 100
        world.add_run("SUCCEEDED", labels=stand_down, output={"deployed": False})
    assert await world.tick() == f"up to date at {SHA}"
    assert not world.deployed


async def test_the_release_is_the_pair_the_run_says_it_deployed(world):
    # A run that followed a moved head is labelled with the pair it was submitted for.
    world.add_run(
        "SUCCEEDED",
        labels=_deployer._pair_labels(world.profile, OLD_SHA, ""),
        output={"deployed": True, "envs": ["factory"], "sha": SHA, "direction_sha": ""},
    )
    assert await world.tick() == f"up to date at {SHA}"


@both_paths
async def test_a_pair_that_never_left_the_spot_queue_is_not_an_attempt(world):
    labels = _deployer._pair_labels(world.profile, SHA, "")
    for _ in range(_deployer.MAX_ATTEMPTS):
        world.add_run("TIMED_OUT", labels=labels, started=False)
    assert (await world.tick()).startswith(f"deployed {SHA}")
    assert world.last["sha"] == SHA


@both_paths
async def test_a_pair_killed_after_it_started_is_an_attempt(world):
    labels = _deployer._pair_labels(world.profile, SHA, "")
    world.add_run("TIMED_OUT", labels=labels, started=False)
    for _ in range(_deployer.MAX_ATTEMPTS):
        world.add_run("TIMED_OUT", labels=labels)
    assert "giving up" in await world.tick()
    assert not world.deployed


_LABEL_VALUE = re.compile(r"([A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9])?)?")


@pytest.mark.parametrize("consistent", [False, True], ids=["git-ref", "listing"])
async def test_every_label_value_the_deployer_sets_is_a_kubernetes_label_value(
    directed, monkeypatch, consistent
):
    # Run labels may become pod labels: at most 63 characters, alphanumeric at both ends.
    monkeypatch.setattr(_deployer, "RUN_LISTING_CONSISTENT", consistent)
    # A profile name may be any string, and is not itself a label value.
    text = directed.profile.text.replace('name = "work"', 'name = "Work + Ops/EU"', 1)
    directed.profile = _profile.parse(text, "work+direction")
    directed.ship_direction_profile()
    await directed.tick()
    directed.gh.head_sha = NEW_SHA
    await directed.tick()
    values = [v for labels in directed.labels for v in labels.values()]
    values += _deployer.release_labels(directed.profile).values()
    values += _deployer._pair_labels(directed.profile, SHA, "").values()
    assert len(directed.labels) == 2 and values
    for value in values:
        assert len(value) <= 63 and _LABEL_VALUE.fullmatch(value), value


def test_the_repository_label_is_a_32_character_prefix(world):
    repo = _deployer.release_labels(world.profile)["deploy-repo"]
    assert len(repo) == 32 and hashlib.sha256(b"acme/factory").hexdigest().startswith(repo)
