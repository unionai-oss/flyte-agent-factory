"""A capacity change wakes the deployed admission path without waiting for a timer."""

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import cast

import flyte
import pytest
from flyte import remote

from flyte_agent_factory import (
    _deploy,
    _deployer,
    _factory,
    _factory_base,
    _factory_meters,
    _planner_runtime,
    _profile,
)
from flyte_agent_factory._board import BoardClient
from flyte_agent_factory._github import GithubApp
from tests.fake_flyte import FakeFlyte
from tests.test_deployer import NEW_SHA, SHA
from tests.test_deployer import world as world
from tests.test_webhook import intake as intake


@pytest.mark.parametrize("admission", ["legacy", "planner"])
@pytest.mark.parametrize("changed", [False, True])
async def test_slot_change_wakes_once_across_deploys(world, monkeypatch, admission, changed):
    world.profile = dataclasses.replace(world.profile, planner=_profile.Planner(admission=admission))
    shipped = world.profile.text
    if admission == "planner":
        shipped += '\n[planner]\nadmission = "planner"\n'
    if changed:
        shipped = shipped.replace('"agent-two"', '"agent-three"')
    world.ship_profile(shipped)
    wakes = []
    submit = _deployer._submit

    async def dispatch(task_name, **kwargs):
        if task_name == "factory.sweep":
            wakes.append(task_name)
            return SimpleNamespace(name="sweep")
        return await submit(task_name, **kwargs)

    monkeypatch.setattr(_deployer, "_submit", dispatch)
    monkeypatch.setattr(
        _planner_runtime, "notify", lambda profile, key: wakes.append(profile.planner.admission)
    )
    monkeypatch.setenv(_deploy.GITHUB_KEY_ENV, "offline-app-key")
    await world.tick()
    assert wakes == (["factory.sweep" if admission == "legacy" else "planner"] if changed else [])
    world.gh.head_sha = NEW_SHA
    await world.tick()
    assert len(wakes) == int(changed)
    assert world.last["sha"] == NEW_SHA


async def test_added_slot_dispatches_waiting_legacy_launch(world, monkeypatch):
    deployed = world.profile
    world.profile = dataclasses.replace(deployed, slots=deployed.slots[:1])
    world.ship_profile(deployed.text)
    with monkeypatch.context() as patch:
        fake = FakeFlyte(patch, _factory)
        patch.setattr(_factory_base, "PROFILE", world.profile)
        patch.setattr(_factory_base, "SLOTS", ("agent-one.project",))
        fake.add("busy", "agent-one.project")
        assert _factory.launch_project("build it", "waiting")["status"] == "queued"
    submit = _deployer._submit
    wakes = []

    async def dispatch(task_name, **kwargs):
        if task_name != "factory.sweep":
            return await submit(task_name, **kwargs)
        wakes.append(task_name)
        assert world.factory_calls
        with monkeypatch.context() as patch:
            FakeFlyte(patch, _factory)
            patch.setattr(remote.Run, "listall", fake._listall)
            patch.setattr(remote.Run, "get", fake._get)
            patch.setattr(flyte, "with_runcontext", fake._with_runcontext)
            patch.setattr(_factory_base, "PROFILE", deployed)
            patch.setattr(_factory_base, "SLOTS", ("agent-one.project", "agent-two.project"))
            assert _factory.dispatch()
        return SimpleNamespace(name="sweep")

    monkeypatch.setattr(_deployer, "_submit", dispatch)
    await world.tick()
    assert wakes == ["factory.sweep"]
    assert [entry["task"] for entry in fake.created] == ["agent-two.project"]
    assert world.last["sha"] == SHA


def fake_runs(fake: FakeFlyte) -> list[remote.Run]:
    # FakeRun stands in for remote.Run in everything the planner reads.
    return cast("list[remote.Run]", list(fake.runs.values()))


async def test_added_slot_wakes_signed_planner_and_dispatches_waiting_launch(intake, world, monkeypatch):
    import asyncio

    import httpx

    from flyte_agent_factory import _planner, _planner_facts, _queue, _webhook
    from tests.test_planner_admission import Git

    text = world.profile.text + '\n[planner]\nadmission = "planner"\n'
    deployed = _profile.parse(text)
    world.profile = dataclasses.replace(deployed, slots=deployed.slots[:1])
    world.ship_profile(text)
    with monkeypatch.context() as patch:
        fake = FakeFlyte(patch, _factory)
        patch.setattr(_factory_base, "PROFILE", world.profile)
        fake.add("busy", "agent-one.project")
        patch.setattr(_factory, "_resolve_repo", lambda repo, *args, **kwargs: repo)
        assert _planner_facts.free_capacity(world.profile, fake_runs(fake), {}) == []
        request = _factory.prepare_launch("build it", "waiting", repo="team/repo").request
        _queue.enqueue(_factory_base.queue_root(), request)
    wakes = []
    git = Git()
    monkeypatch.setattr(git, "default_branch_sha", lambda *_: ("main", "base"), raising=False)

    def consume():
        wakes.append(True)
        assert world.factory_calls
        with monkeypatch.context() as patch:
            patch.setattr(remote.Run, "listall", fake._listall)
            patch.setattr(remote.Run, "get", fake._get)
            patch.setattr(remote.Task, "get", lambda name, **_: name)
            patch.setattr(flyte, "with_runcontext", fake._with_runcontext)
            patch.setattr(_factory_base, "PROFILE", deployed)
            patch.setattr(_factory_base, "SLOTS", ("agent-one.project", "agent-two.project"))
            patch.setattr(_factory_meters, "slot_headroom", lambda _: {})
            patch.setattr(_factory, "_resolve_repo", lambda repo, *args, **kwargs: repo)
            patch.setattr(_planner_runtime, "_intents", lambda *_: [])
            facts = _planner.Facts(
                manual_requests=(_planner.ManualRequest("waiting", "team/repo", body="build it"),),
                capacity=_planner_facts.free_capacity(deployed, fake_runs(fake), {}),
            )
            assert _planner_runtime.execute(
                deployed,
                cast(GithubApp, git),  # the test Git implements only the calls the planner makes
                "team/direction",
                "base",
                cast(BoardClient, None),  # no board in this scenario
                [],
                facts,
                {"waiting": request},
            )

    monkeypatch.setattr(_webhook, "PROFILE", deployed)
    monkeypatch.setattr(_webhook, "_consumer", SimpleNamespace(wake=consume))
    monkeypatch.setenv(_deploy.GITHUB_KEY_ENV, "offline-app-key")
    monkeypatch.setattr(remote.App, "get", lambda **_: SimpleNamespace(endpoint="http://test"))
    posts = []

    def post(url, *, content, headers, timeout):
        posts.append(url)
        assert timeout == 10
        return asyncio.run(intake.client.post(url, content=content, headers=headers))

    monkeypatch.setattr(httpx, "post", post)
    await world.tick()
    assert posts == ["http://test/internal/planner"]
    assert wakes == [True]
    assert [entry["task"] for entry in fake.created] == ["agent-two.project"]


async def test_followed_head_with_same_slots_does_not_repeat_wake(world, monkeypatch):
    world.ship_profile(world.profile.text.replace('"agent-two"', '"agent-three"'))
    deploy = _deploy.deploy_in_tree

    def move_head(*args, **kwargs):
        result = deploy(*args, **kwargs)
        world.gh.head_sha = NEW_SHA
        return result

    monkeypatch.setattr(_deploy, "deploy_in_tree", move_head)
    wakes = []
    submit = _deployer._submit

    async def dispatch(task_name, **kwargs):
        if task_name == "factory.sweep":
            wakes.append(task_name)
            return SimpleNamespace(name="sweep")
        return await submit(task_name, **kwargs)

    monkeypatch.setattr(_deployer, "_submit", dispatch)
    await world.tick()
    assert len(world.factory_calls) == 2
    assert wakes == ["factory.sweep"]
    assert world.last["sha"] == NEW_SHA


@pytest.mark.parametrize("admission", ["legacy", "planner"])
async def test_failed_wake_does_not_retry_a_successful_deploy(world, monkeypatch, admission):
    text = world.profile.text.replace('"agent-two"', '"agent-three"')
    text += f'\n[planner]\nadmission = "{admission}"\n'
    world.ship_profile(text)
    wakes = []
    submit = _deployer._submit

    def unavailable(*_):
        wakes.append(True)
        raise OSError("offline")

    async def dispatch(task_name, **kwargs):
        if task_name == "factory.sweep":
            unavailable()
        return await submit(task_name, **kwargs)

    monkeypatch.setenv(_deploy.GITHUB_KEY_ENV, "offline-app-key")
    monkeypatch.setattr(_deployer, "_submit", dispatch)
    monkeypatch.setattr(_planner_runtime, "notify", unavailable)
    await world.tick()
    assert wakes == [True]
    assert world.last["sha"] == SHA
    assert await world.tick() == f"up to date at {SHA}"
    assert len(wakes) == 1
    assert any("dispatch wake failed" in message for message in world.said)


async def test_failed_deploy_does_not_wake_dispatch(world, monkeypatch):
    world.ship_profile(world.profile.text.replace('"agent-two"', '"agent-three"'))
    world.factory_rc = 1

    async def unexpected(_):
        pytest.fail("an unsuccessful deploy cannot announce new capacity")

    monkeypatch.setattr(_deployer, "_wake_dispatch", unexpected)
    with pytest.raises(_deploy.DeployError, match="factory deploy"):
        await world.tick()
    assert world.last is None


@pytest.mark.parametrize("admission", ["planner"])
async def test_a_stalled_planner_wake_does_not_hold_the_deploy(world, monkeypatch, admission):
    # QA: notify's App lookup has no client timeout; a stalled read held the deploy until its task timeout.
    import threading

    text = world.profile.text.replace('"agent-two"', '"agent-three"')
    text += f'\n[planner]\nadmission = "{admission}"\n'
    world.ship_profile(text)
    stalled = threading.Event()

    def hang(*_):
        stalled.wait(30)

    monkeypatch.setenv(_deploy.GITHUB_KEY_ENV, "offline-app-key")
    monkeypatch.setattr(_planner_runtime, "notify", hang)
    monkeypatch.setattr(_deployer, "WAKE_TIMEOUT_S", 0.2)
    try:
        await asyncio.wait_for(world.tick(), 30)
        assert world.last["sha"] == SHA  # the release is recorded although the wake never returned
        assert any("dispatch wake failed" in message for message in world.said)
    finally:
        stalled.set()
